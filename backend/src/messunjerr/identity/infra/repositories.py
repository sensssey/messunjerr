"""Репозитории identity: тонкая обёртка над `AsyncSession`. `commit()` они не вызывают (4.3)."""

import uuid
from datetime import datetime

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from messunjerr.identity.infra.models import EmailTokenRow, SessionRow, UserRow


def violated_constraint(error: IntegrityError) -> str | None:
    """Имя нарушенного ограничения PostgreSQL (`uq_users_email` и т.п.) или `None`."""
    original: BaseException | None = error.orig
    while original is not None:
        name = getattr(original, "constraint_name", None)
        if isinstance(name, str):
            return name
        original = original.__cause__
    return None


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, user: UserRow) -> None:
        self._session.add(user)

    async def get_by_id(self, user_id: uuid.UUID, *, for_update: bool = False) -> UserRow | None:
        statement = select(UserRow).where(UserRow.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_email(self, email: str, *, for_update: bool = False) -> UserRow | None:
        statement = select(UserRow).where(UserRow.email == email)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_username(self, username: str) -> UserRow | None:
        statement = select(UserRow).where(UserRow.username == username)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_login(self, login: str) -> UserRow | None:
        """Вход по почте или по нику: адрес всегда содержит `@`, ник его содержать не может."""
        if "@" in login:
            return await self.get_by_email(login)
        return await self.get_by_username(login)

    async def delete_stale_pending(
        self, *, cutoff: datetime, now: datetime, limit: int
    ) -> list[uuid.UUID]:
        """Удаляет не более `limit` неподтверждённых аккаунтов; возвращает их `id`.

        Берутся аккаунты `pending` без подтверждённой почты, чьи данные не менялись с `cutoff`
        (повторная регистрация обновляет `updated_at`) и у которых нет живого токена подтверждения.
        Строки выбираются с `SKIP LOCKED`: второй воркер не ждёт первого. Сессии и токены уходят
        каскадом.
        """
        user = aliased(UserRow)
        live_token = exists().where(
            EmailTokenRow.user_id == user.id,
            EmailTokenRow.consumed_at.is_(None),
            EmailTokenRow.expires_at > now,
        )
        stale = (
            select(user.id)
            .where(
                user.status == "pending",
                user.email_verified_at.is_(None),
                user.updated_at < cutoff,
                ~live_token,
            )
            .order_by(user.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=user)
        )
        result = await self._session.execute(
            delete(UserRow)
            .where(UserRow.id.in_(stale))
            .returning(UserRow.id)
            .execution_options(synchronize_session=False)
        )
        return list(result.scalars())


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: SessionRow) -> None:
        self._session.add(row)

    async def get(self, session_id: uuid.UUID, *, for_update: bool = False) -> SessionRow | None:
        statement = select(SessionRow).where(SessionRow.id == session_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_refresh_hash(
        self, token_hash: bytes, *, for_update: bool = False
    ) -> SessionRow | None:
        """Сессия, у которой этот токен текущий."""
        statement = select(SessionRow).where(SessionRow.refresh_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_prev_refresh_hash(
        self, token_hash: bytes, *, for_update: bool = False
    ) -> SessionRow | None:
        """Сессия, у которой этот токен был текущим до последней ротации."""
        statement = select(SessionRow).where(SessionRow.prev_refresh_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_active(self, user_id: uuid.UUID, now: datetime) -> list[SessionRow]:
        """Действующие сессии пользователя: не отозваны, оба срока не вышли; свежие сверху."""
        statement = (
            select(SessionRow)
            .where(
                SessionRow.user_id == user_id,
                SessionRow.revoked_at.is_(None),
                SessionRow.expires_at > now,
                SessionRow.absolute_expires_at > now,
            )
            .order_by(SessionRow.last_seen_at.desc(), SessionRow.id.desc())
        )
        return list((await self._session.execute(statement)).scalars())

    async def revoke_all(
        self, user_id: uuid.UUID, *, reason: str, now: datetime, keep: uuid.UUID | None = None
    ) -> list[uuid.UUID]:
        """Отзывает все действующие сессии пользователя (кроме `keep`); возвращает их `id`."""
        statement = (
            update(SessionRow)
            .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
            .returning(SessionRow.id)
        )
        if keep is not None:
            statement = statement.where(SessionRow.id != keep)
        # synchronize_session не нужен: загруженные объекты сессий в этих командах не используются.
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return list(result.scalars())


class EmailTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: EmailTokenRow) -> None:
        self._session.add(row)

    async def get_active_for_update(
        self, token_hash: bytes, purpose: str, now: datetime
    ) -> EmailTokenRow | None:
        """Непогашенный и непросроченный токен; строка блокируется до конца транзакции."""
        statement = (
            select(EmailTokenRow)
            .where(
                EmailTokenRow.token_hash == token_hash,
                EmailTokenRow.purpose == purpose,
                EmailTokenRow.consumed_at.is_(None),
                EmailTokenRow.expires_at > now,
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def invalidate_active(self, user_id: uuid.UUID, purpose: str, now: datetime) -> None:
        """Гасит прежние непогашенные токены того же назначения: действует только последний."""
        await self._session.execute(
            update(EmailTokenRow)
            .where(
                EmailTokenRow.user_id == user_id,
                EmailTokenRow.purpose == purpose,
                EmailTokenRow.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

    async def delete_spent(self, *, cutoff: datetime, limit: int) -> int:
        """Удаляет не более `limit` токенов, просроченных или использованных до `cutoff`."""
        spent = aliased(EmailTokenRow)
        stale = (
            select(spent.id)
            .where(or_(spent.expires_at < cutoff, spent.consumed_at < cutoff))
            .limit(limit)
            .with_for_update(skip_locked=True, of=spent)
        )
        result = await self._session.execute(
            delete(EmailTokenRow)
            .where(EmailTokenRow.id.in_(stale))
            .returning(EmailTokenRow.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())

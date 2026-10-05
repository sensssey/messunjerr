"""Репозитории identity: тонкая обёртка над `AsyncSession`. `commit()` они не вызывают (4.3)."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: SessionRow) -> None:
        self._session.add(row)


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

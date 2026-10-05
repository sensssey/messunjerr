"""Команда `POST /auth/refresh` (4.7): ротация refresh-токена и защита от его повторного использования.

Алгоритм:

1. По SHA-256 токена ищется сессия, у которой он текущий (`FOR UPDATE`). Если она действует, токен
   заменяется новым, предыдущий запоминается в `prev_refresh_hash`, срок скользит на 30 дней, но не
   дальше предела в 90 дней.
2. Иначе ищется сессия, у которой токен был предыдущим. Если с ротации прошло не больше 10 секунд, это
   гонка двух вкладок: выдаём только новый access-токен, cookie не трогаем (браузер уже получил свежую
   из первого ответа). Позже: токен украден или повторён. Сессия отзывается, `sid` уходит в denylist,
   пишется аудит, владельцу уходит письмо, ответ `401 refresh_reused`.
3. Нигде не найден: `401 refresh_invalid`.

Побочные эффекты при отказе (отзыв сессии, аудит) фиксируются коммитом до выброса ошибки, иначе откат
транзакции стёр бы их.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.ratelimit import RateLimiter, rate_limited
from messunjerr.core.security import hash_token, new_opaque_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    ensure_can_sign_in,
    revoke_in_denylist_after_commit,
)
from messunjerr.identity.commands.mail import send_refresh_reuse_notice
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import (
    refresh_expired,
    refresh_invalid,
    refresh_missing,
    refresh_reused,
)
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.models import SessionRow, UserRow
from messunjerr.identity.infra.repositories import SessionRepository, UserRepository
from messunjerr.identity.infra.session_denylist import SessionDenylist
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings

REFRESH_BUCKET = "auth_refresh_session"


@dataclass(frozen=True, slots=True)
class Refresh:
    token: str | None
    """Refresh-токен из cookie или `None`, если cookie не пришла."""
    client: ClientInfo


@dataclass(frozen=True, slots=True)
class Refreshed:
    user: MeUser
    session_id: uuid.UUID
    access_token: str
    expires_in: int
    new_refresh_token: str | None
    """`None` в окне гонки вкладок: cookie уже обновлена первым ответом и не меняется."""
    refresh_max_age: int


def _ensure_usable(session: SessionRow, now: datetime) -> None:
    if session.revoked_at is not None:
        raise refresh_invalid()
    if now >= session.expires_at or now >= session.absolute_expires_at:
        raise refresh_expired()


async def _user_of(users: UserRepository, session: SessionRow) -> UserRow:
    """Пользователь сессии; статус перечитывается из БД (блокировка действует и на refresh)."""
    user = await users.get_by_id(session.user_id)
    if user is None:
        raise refresh_invalid()
    ensure_can_sign_in(user)
    return user


async def _enforce_limit(limiter: RateLimiter, session: SessionRow) -> None:
    result = await limiter.consume(REFRESH_BUCKET, str(session.id))
    if not result.allowed:
        raise rate_limited(result)


async def refresh_session(
    command: Refresh,
    *,
    uow: UnitOfWork,
    tokens: TokenService,
    denylist: SessionDenylist,
    limiter: RateLimiter,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> Refreshed:
    moment = now or utcnow()
    if not command.token:
        raise refresh_missing()

    digest = hash_token(command.token)
    sessions = SessionRepository(uow.session)
    users = UserRepository(uow.session)

    session = await sessions.get_by_refresh_hash(digest, for_update=True)
    if session is not None:
        _ensure_usable(session, moment)
        await _enforce_limit(limiter, session)
        user = await _user_of(users, session)

        new_token = new_opaque_token()
        session.prev_refresh_hash = session.refresh_hash
        session.refresh_hash = hash_token(new_token)
        session.rotated_at = moment
        session.last_seen_at = moment
        session.expires_at = min(
            moment + timedelta(days=settings.refresh_ttl_days), session.absolute_expires_at
        )
        issued = tokens.issue(user_id=user.id, session_id=session.id, role=user.role, now=moment)
        await uow.commit()
        return Refreshed(
            user=MeUser.from_row(user),
            session_id=session.id,
            access_token=issued.token,
            expires_in=issued.expires_in,
            new_refresh_token=new_token,
            refresh_max_age=int((session.expires_at - moment).total_seconds()),
        )

    previous = await sessions.get_by_prev_refresh_hash(digest, for_update=True)
    if previous is None:
        raise refresh_invalid()
    _ensure_usable(previous, moment)
    await _enforce_limit(limiter, previous)
    user = await _user_of(users, previous)

    race_window = timedelta(seconds=settings.refresh_race_window_seconds)
    if previous.rotated_at is not None and moment - previous.rotated_at <= race_window:
        # Две вкладки обновили токен почти одновременно: вторая получает только новый access.
        previous.last_seen_at = moment
        issued = tokens.issue(user_id=user.id, session_id=previous.id, role=user.role, now=moment)
        await uow.commit()
        return Refreshed(
            user=MeUser.from_row(user),
            session_id=previous.id,
            access_token=issued.token,
            expires_in=issued.expires_in,
            new_refresh_token=None,
            refresh_max_age=int((previous.expires_at - moment).total_seconds()),
        )

    # Токен предъявлен повторно после окна гонки: считаем его украденным и закрываем сессию.
    previous.revoked_at = moment
    previous.revoked_reason = "reuse_detected"
    record_audit(
        uow.session,
        action=audit.REFRESH_REUSE_DETECTED,
        actor_id=user.id,
        target_type=audit.TARGET_SESSION,
        target_id=previous.id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
    )
    revoke_in_denylist_after_commit(uow, denylist, [previous.id])
    uow.after_commit(
        lambda: send_refresh_reuse_notice(
            jobs=jobs, settings=settings, user=user, device=previous.device_label
        )
    )
    await uow.commit()
    raise refresh_reused()

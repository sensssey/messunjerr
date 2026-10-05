"""Сброс и смена пароля (5.2): `forgot`, `reset`, `change`.

- `forgot` отвечает `202` всегда; письмо уходит только существующему и не заблокированному аккаунту, а
  вся работа выполняется после отправки ответа, чтобы время не выдавало, есть ли адрес.
- `reset` (по токену из письма) задаёт пароль, отзывает все сессии и подтверждает почту: человек,
  открывший письмо, владеет ящиком. Так тот, кто занял чужую почту, не удержит аккаунт.
- `change` (с токеном доступа и паролем) отзывает остальные сессии, текущая остаётся.

Оба изменения пароля пишут аудит и событие `PasswordChanged` и шлют владельцу уведомление.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.ratelimit import RateLimiter
from messunjerr.core.security import hash_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    password_policy_error,
    revoke_in_denylist_after_commit,
    verify_reauth,
)
from messunjerr.identity.commands.logout import Actor
from messunjerr.identity.commands.mail import (
    PURPOSE_RESET_PASSWORD,
    PURPOSE_VERIFY_EMAIL,
    issue_password_reset_email,
    send_password_changed_notice,
)
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import token_invalid_or_expired, unauthorized
from messunjerr.identity.domain.events import EmailVerified, PasswordChanged, record
from messunjerr.identity.domain.passwords import PasswordProblem, check_password_policy
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.repositories import (
    EmailTokenRepository,
    SessionRepository,
    UserRepository,
)
from messunjerr.identity.infra.session_denylist import SessionDenylist
from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class ResetPassword:
    token: str
    new_password: str
    client: ClientInfo


@dataclass(frozen=True, slots=True)
class ChangePassword:
    actor: Actor
    current_password: str
    new_password: str
    revoke_other_sessions: bool
    client: ClientInfo


def _weak_password(problem: PasswordProblem) -> DomainError:
    return DomainError(
        ErrorCode.VALIDATION_ERROR, errors=[password_policy_error(problem, "/body/new_password")]
    )


# ----------------------------------------------------------------------------- forgot
async def request_password_reset(
    email: str,
    *,
    uow: UnitOfWork,
    jobs: JobQueue,
    settings: Settings,
    client: ClientInfo,
    now: datetime | None = None,
) -> bool:
    """Выдаёт токен сброса и ставит письмо; `True`, если письмо поставлено. Заблокированным не шлём."""
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_email(email, for_update=True)
    if user is None or user.status == "banned":
        return False
    await issue_password_reset_email(
        session=uow.session, jobs=jobs, settings=settings, user=user, now=moment
    )
    record_audit(
        uow.session,
        action=audit.PASSWORD_RESET_REQUESTED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    await uow.commit()
    return True


async def request_password_reset_after_response(
    email: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    settings: Settings,
    client: ClientInfo,
) -> None:
    """Обёртка для фоновой задачи запроса: ответ уже ушёл, ошибки идут только в журнал (без адреса)."""
    try:
        async with UnitOfWork(sessionmaker) as uow:
            await request_password_reset(
                email, uow=uow, jobs=jobs, settings=settings, client=client
            )
    except Exception as error:
        get_logger("messunjerr.identity").error(
            "password_reset_request_failed", error_type=type(error).__name__
        )


# ----------------------------------------------------------------------------- reset
async def reset_password(
    command: ResetPassword,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    denylist: SessionDenylist,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Порядок дешёвого к дорогому: токен, правила пароля, и только потом Argon2id (хэш на каждый
    запрос с выдуманным токеном был бы подарком для DoS)."""
    moment = now or utcnow()
    tokens = EmailTokenRepository(uow.session)
    token_row = await tokens.get_active_for_update(
        hash_token(command.token), PURPOSE_RESET_PASSWORD, moment
    )
    if token_row is None:
        raise token_invalid_or_expired()
    user = await UserRepository(uow.session).get_by_id(token_row.user_id, for_update=True)
    if user is None:
        raise token_invalid_or_expired()

    problem = check_password_policy(command.new_password, username=user.username, email=user.email)
    if problem is not None:
        raise _weak_password(problem)

    user.password_hash = await passwords.hash(command.new_password)
    token_row.consumed_at = moment
    # Токен пришёл на почту аккаунта: владение ящиком доказано, неподтверждённый аккаунт активируется.
    if user.email_verified_at is None:
        user.email_verified_at = moment
        await tokens.invalidate_active(user.id, PURPOSE_VERIFY_EMAIL, moment)
        record(uow.outbox, EmailVerified(user.id))
    if user.status == "pending":
        user.status = "active"

    revoked = await SessionRepository(uow.session).revoke_all(
        user.id, reason="password_changed", now=moment
    )
    record(uow.outbox, PasswordChanged(user.id))
    record_audit(
        uow.session,
        action=audit.PASSWORD_RESET,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
        data={"revoked_sessions": len(revoked)},
    )
    revoke_in_denylist_after_commit(uow, denylist, revoked)
    _notify_password_changed(uow, jobs, settings, user, moment)
    await uow.commit()


def _notify_password_changed(
    uow: UnitOfWork, jobs: JobQueue, settings: Settings, user: UserRow, moment: datetime
) -> None:
    uow.after_commit(
        lambda: send_password_changed_notice(jobs=jobs, settings=settings, user=user, now=moment)
    )


# ----------------------------------------------------------------------------- change
async def change_password(
    command: ChangePassword,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    limiter: RateLimiter,
    denylist: SessionDenylist,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_id(command.actor.user_id, for_update=True)
    if user is None:
        raise unauthorized(ErrorCode.TOKEN_INVALID, "The user of this token no longer exists.")
    await verify_reauth(
        uow=uow,
        user=user,
        password=command.current_password,
        passwords=passwords,
        limiter=limiter,
        client=command.client,
    )

    problem = check_password_policy(command.new_password, username=user.username, email=user.email)
    if problem is not None:
        raise _weak_password(problem)
    current_hash = user.password_hash  # после verify_reauth он есть и, если нужно, обновлён
    if (
        current_hash is not None
        and (await passwords.verify(command.new_password, current_hash)).valid
    ):
        raise _weak_password(PasswordProblem.SAME_AS_CURRENT)

    user.password_hash = await passwords.hash(command.new_password)
    revoked: list[uuid.UUID] = []
    if command.revoke_other_sessions:
        revoked = await SessionRepository(uow.session).revoke_all(
            user.id, reason="password_changed", now=moment, keep=command.actor.session_id
        )
    record(uow.outbox, PasswordChanged(user.id))
    record_audit(
        uow.session,
        action=audit.PASSWORD_CHANGED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
        data={"revoked_sessions": len(revoked)},
    )
    revoke_in_denylist_after_commit(uow, denylist, revoked)
    _notify_password_changed(uow, jobs, settings, user, moment)
    await uow.commit()

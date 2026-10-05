"""Команда `POST /auth/login` (5.2): проверка пароля и статуса, выдача сессии.

Защита от перебора (4.14): лимит по адресу (`auth_login_ip`, в зависимости ручки) и лимит неудачных
попыток по аккаунту (`auth_login_account`). Токен аккаунта забирается до проверки пароля и
возвращается при успехе: параллельные догадки не проскакивают, пока первая ещё считается.
Каждая попытка, удачная и нет, пишется в аудит.
"""

from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.errors import DomainError
from messunjerr.core.ratelimit import RateLimiter, rate_limited
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ACCOUNT_BUCKET,
    ClientInfo,
    SignedIn,
    account_subject,
    ensure_can_sign_in,
    start_session,
)
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import invalid_credentials
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.repositories import UserRepository
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings

_STATUS_REASONS = {
    "email_not_verified": "not_verified",
    "account_suspended": "suspended",
    "account_banned": "banned",
}


@dataclass(frozen=True, slots=True)
class Login:
    login: str
    """Почта или ник."""
    password: str
    client: ClientInfo


async def _fail(
    uow: UnitOfWork,
    command: Login,
    user: UserRow | None,
    reason: str,
    error: DomainError,
) -> DomainError:
    """Фиксирует запись аудита о неудачной попытке (отдельно от остального) и отдаёт ошибку."""
    record_audit(
        uow.session,
        action=audit.LOGIN_FAILURE,
        actor_id=user.id if user is not None else None,
        target_type=audit.TARGET_USER if user is not None else None,
        target_id=user.id if user is not None else None,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
        data={"reason": reason},
    )
    await uow.commit()
    return error


async def login(
    command: Login,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    tokens: TokenService,
    limiter: RateLimiter,
    settings: Settings,
    now: datetime | None = None,
) -> SignedIn:
    """Порядок важен: статус аккаунта сообщается только после верного пароля.

    Для несуществующего логина и для аккаунта без пароля тратится столько же времени, сколько на
    проверку настоящего пароля, а ответ одинаков (`invalid_credentials`).
    """
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_login(command.login)

    subject = account_subject(user, command.login)
    attempt = await limiter.consume(ACCOUNT_BUCKET, subject)
    if not attempt.allowed:
        raise rate_limited(attempt)

    if user is None or user.password_hash is None:
        await passwords.burn(command.password)
        raise await _fail(uow, command, user, "unknown_login", invalid_credentials())

    check = await passwords.verify(command.password, user.password_hash)
    if not check.valid:
        raise await _fail(uow, command, user, "bad_password", invalid_credentials())

    # Пароль верен: попытка не считается подбором, токен возвращается (даже если статус не пускает).
    await limiter.refund(ACCOUNT_BUCKET, subject)
    try:
        ensure_can_sign_in(user)
    except DomainError as error:
        raise await _fail(
            uow, command, user, _STATUS_REASONS.get(error.code.value, "status"), error
        ) from error

    if check.new_hash is not None:
        user.password_hash = check.new_hash  # параметры Argon2id изменились: обновляем хэш
    grant = start_session(
        session=uow.session,
        tokens=tokens,
        settings=settings,
        user=user,
        client=command.client,
        now=moment,
    )
    user.last_login_at = moment
    record_audit(
        uow.session,
        action=audit.LOGIN_SUCCESS,
        actor_id=user.id,
        target_type=audit.TARGET_SESSION,
        target_id=grant.session_id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
    )
    await uow.commit()
    return SignedIn(user=MeUser.from_row(user), grant=grant)

"""Общее для команд identity: сведения о клиенте, выдача сессии, допуск по статусу, повторный ввод
пароля и отзыв сессий."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.audit import record_audit
from messunjerr.core.codes import ItemCode
from messunjerr.core.errors import ErrorItem
from messunjerr.core.ids import uuid7
from messunjerr.core.ratelimit import RateLimiter, rate_limited
from messunjerr.core.ratelimit_deps import subject_digest
from messunjerr.core.security import hash_token, new_opaque_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import (
    account_banned,
    account_suspended,
    email_not_verified,
    reauth_failed,
)
from messunjerr.identity.domain.passwords import PasswordProblem
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.models import SessionRow, UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.session_denylist import SessionDenylist
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings

USER_AGENT_MAX_LENGTH = 512
DEVICE_LABEL_MAX_LENGTH = 100
ACCOUNT_BUCKET = "auth_login_account"

_PASSWORD_ITEM_CODES = {
    PasswordProblem.TOO_SHORT: ItemCode.STRING_TOO_SHORT,
    PasswordProblem.TOO_LONG: ItemCode.STRING_TOO_LONG,
}


def password_policy_error(problem: PasswordProblem, pointer: str) -> ErrorItem:
    """Элемент `errors[]` для нарушенного правила пароля; причина в `meta.reason`."""
    return ErrorItem(
        pointer,
        _PASSWORD_ITEM_CODES.get(problem, ItemCode.PASSWORD_TOO_WEAK),
        "The password does not meet the requirements.",
        {"reason": problem.value},
    )


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """Откуда пришёл запрос. IP и User-Agent хранятся в сессии не дольше `IP_RETENTION_DAYS` (⚖️)."""

    ip: str | None = None
    user_agent: str | None = None
    device_label: str | None = None


@dataclass(frozen=True, slots=True)
class SessionGrant:
    """Результат входа: access-токен для тела ответа и refresh-токен для cookie."""

    session_id: uuid.UUID
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_max_age: int


@dataclass(frozen=True, slots=True)
class SignedIn:
    """Вход выполнен: пользователь и выданная сессия (ответ `login` и `verify-email`)."""

    user: MeUser
    grant: SessionGrant


def ensure_can_sign_in(user: UserRow) -> None:
    """Допуск по статусу (5.2). `deletion_pending` входить может: клиент предложит восстановление."""
    match user.status:
        case "pending":
            raise email_not_verified()
        case "suspended":
            raise account_suspended(user.suspended_until)
        case "banned":
            raise account_banned()
        case _:
            pass  # active, deletion_pending


def account_subject(user: UserRow | None, login: str) -> str:
    """Субъект лимита попыток по аккаунту. По `id`, если аккаунт есть: чередуя почту и ник, лимит
    не обойти. Для несуществующего логина по хэшу логина: поведение одинаково (4.14)."""
    return f"u:{user.id}" if user is not None else f"l:{subject_digest(login)}"


def revoke_in_denylist_after_commit(
    uow: UnitOfWork, denylist: SessionDenylist, session_ids: Sequence[uuid.UUID]
) -> None:
    """После коммита добавляет сессии в denylist Redis: их access-токены перестают приниматься сразу.

    Запись в БД (`revoked_at`) уже сделана и надёжна; сбой Redis только оставляет выданным токенам
    их оставшиеся минуты жизни, UnitOfWork пишет такой сбой в журнал.
    """
    ids = list(session_ids)
    if ids:
        uow.after_commit(lambda: denylist.revoke(ids))


async def verify_reauth(
    *,
    uow: UnitOfWork,
    user: UserRow,
    password: str,
    passwords: PasswordService,
    limiter: RateLimiter,
    client: ClientInfo,
) -> None:
    """Повторный ввод пароля перед чувствительным действием (4.7).

    Неудачные попытки считает тот же бакет, что и вход по паролю (`auth_login_account`): подбор
    пароля через украденный access-токен упирается в тот же лимит. Токен забирается заранее и
    возвращается при успехе, поэтому параллельные запросы лимит не обходят. Неверный пароль:
    запись в аудит фиксируется отдельно от остальных изменений и только затем `403 reauth_failed`.
    """
    subject = account_subject(user, "")
    result = await limiter.consume(ACCOUNT_BUCKET, subject)
    if not result.allowed:
        raise rate_limited(result)

    if user.password_hash is None:
        await passwords.burn(password)
        valid = False
    else:
        check = await passwords.verify(password, user.password_hash)
        valid = check.valid
        if valid and check.new_hash is not None:
            user.password_hash = check.new_hash

    if not valid:
        record_audit(
            uow.session,
            action=audit.REAUTH_FAILURE,
            actor_id=user.id,
            target_type=audit.TARGET_USER,
            target_id=user.id,
            ip=client.ip,
            user_agent=client.user_agent,
        )
        await uow.commit()
        raise reauth_failed()
    await limiter.refund(ACCOUNT_BUCKET, subject)


def start_session(
    *,
    session: AsyncSession,
    tokens: TokenService,
    settings: Settings,
    user: UserRow,
    client: ClientInfo,
    now: datetime,
) -> SessionGrant:
    """Создаёт сессию и выдаёт пару токенов (ротация и отзыв: `refresh`, `logout`)."""
    refresh_token = new_opaque_token()
    row = SessionRow(
        id=uuid7(),  # нужен до записи в БД: идентификатор сессии (`sid`) попадает в access-токен
        user_id=user.id,
        refresh_hash=hash_token(refresh_token),
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.refresh_ttl_days),
        absolute_expires_at=now + timedelta(days=settings.refresh_absolute_ttl_days),
        ip=client.ip,
        user_agent=client.user_agent[:USER_AGENT_MAX_LENGTH] if client.user_agent else None,
        device_label=client.device_label[:DEVICE_LABEL_MAX_LENGTH] if client.device_label else None,
    )
    session.add(row)
    issued = tokens.issue(user_id=user.id, session_id=row.id, role=user.role, now=now)
    return SessionGrant(
        session_id=row.id,
        access_token=issued.token,
        expires_in=issued.expires_in,
        refresh_token=refresh_token,
        refresh_max_age=settings.refresh_ttl_days * 24 * 3600,
    )

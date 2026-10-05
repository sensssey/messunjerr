"""Общее для команд входа: сведения о клиенте, выдача сессии и допуск по статусу аккаунта."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.ids import uuid7
from messunjerr.core.security import hash_token, new_opaque_token
from messunjerr.identity.domain.errors import account_banned, account_suspended, email_not_verified
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.models import SessionRow, UserRow
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings

USER_AGENT_MAX_LENGTH = 512
DEVICE_LABEL_MAX_LENGTH = 100


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


def start_session(
    *,
    session: AsyncSession,
    tokens: TokenService,
    settings: Settings,
    user: UserRow,
    client: ClientInfo,
    now: datetime,
) -> SessionGrant:
    """Создаёт сессию и выдаёт пару токенов. Ротацию refresh-токена и отзыв добавит S2."""
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

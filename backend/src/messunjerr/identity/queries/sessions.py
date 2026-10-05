"""Список сессий пользователя (`GET /auth/sessions`, 5.2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.schemas import UtcDateTime
from messunjerr.identity.domain.devices import describe_user_agent, mask_ip
from messunjerr.identity.infra.repositories import SessionRepository


class SessionInfo(BaseModel):
    """Одна действующая сессия. Адрес маскируется: полный IP человеку в списке не нужен."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    device_label: str | None
    user_agent: str | None
    ip_masked: str | None
    created_at: UtcDateTime
    last_seen_at: UtcDateTime
    current: bool


async def list_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, current_session_id: uuid.UUID, now: datetime
) -> list[SessionInfo]:
    rows = await SessionRepository(session).list_active(user_id, now)
    return [
        SessionInfo(
            id=row.id,
            # Подпись, которую клиент передал при входе, иначе разбор User-Agent.
            device_label=row.device_label or describe_user_agent(row.user_agent),
            user_agent=row.user_agent,
            ip_masked=mask_ip(str(row.ip) if row.ip is not None else None),
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            current=row.id == current_session_id,
        )
        for row in rows
    ]

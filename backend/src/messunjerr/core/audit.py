"""Журнал аудита (4.14): записи только добавляются, роль `app` не может их править и стирать.

Запись делается в той же транзакции, что и действие. Если действие закончилось ошибкой (неверный
пароль, повторное использование refresh), команда сама фиксирует транзакцию с одной записью аудита
и только потом бросает исключение: иначе откат стёр бы след.

В `data` лежат причины и идентификаторы, но не адреса почты, пароли и токены. IP и User-Agent
хранятся не дольше `IP_RETENTION_DAYS` (⚖️, очистка в S18).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.models import AuditLogRow

USER_AGENT_MAX_LENGTH = 512


def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Добавляет запись в `platform.audit_log` текущей транзакции."""
    session.add(
        AuditLogRow(
            action=action,
            actor_id=actor_id,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip=ip,
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
            data=dict(data or {}),
        )
    )

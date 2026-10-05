"""Запись событий в outbox внутри транзакции (4.3, 4.8).

События описывают факт и содержат только идентификаторы и перечисления: ни email, ни имён, ни
текстов сообщений (⚖️ записи из журнала Kafka выборочно не стереть).
"""

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.ids import uuid7
from messunjerr.core.models import OutboxRow


class Outbox:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(
        self,
        *,
        topic: str,
        key: str,
        event_type: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, Any] | None = None,
    ) -> OutboxRow:
        """Добавляет событие в текущую транзакцию. Публикует его позже ретранслятор (S9)."""
        merged: dict[str, Any] = dict(headers or {})
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        if request_id is not None:
            merged.setdefault("correlation_id", request_id)
        row = OutboxRow(
            event_id=uuid7(),
            topic=topic,
            key=key,
            event_type=event_type,
            payload=dict(payload),
            headers=merged,
        )
        self._session.add(row)
        return row

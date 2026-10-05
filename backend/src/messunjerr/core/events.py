"""Порт шины событий. Реализации приходят вместе с потребителями (Kafka в S9, 4.18)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from messunjerr.core.logs import get_logger


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    """Строка outbox в том виде, в котором её видит ретранслятор."""

    id: int
    event_id: uuid.UUID
    topic: str
    key: str
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    created_at: datetime


class EventBus(Protocol):
    async def publish(self, events: Sequence[OutboundEvent]) -> None:
        """Публикует события; исключение означает «не опубликовано, повторить»."""
        ...


class NullEventBus:
    """Заглушка до S9: ничего не публикует, строки остаются в outbox и копятся."""

    async def publish(self, events: Sequence[OutboundEvent]) -> None:
        get_logger("messunjerr.events").debug("null_event_bus", count=len(events))

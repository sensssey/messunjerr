"""События контекста media (5.15, топик `mj.media.v1`). Только идентификаторы и перечисления (⚖️).

Имя файла сюда не попадает: оно пользовательское и в журнал Kafka его не стереть. До S9 события
копятся в outbox, а задачи (`process_media`, `delete_media_objects`) ставятся напрямую после
коммита; `reconcile_uploads` страхует потерянные постановки (временная схема, план спринтов S5-06).
"""

import uuid
from dataclasses import dataclass

from messunjerr.core.outbox import Outbox

TOPIC_MEDIA = "mj.media.v1"


@dataclass(frozen=True, slots=True)
class AssetUploaded:
    """Клиент завершил загрузку, объект на месте: пора на обработку."""

    asset_id: uuid.UUID
    owner_id: uuid.UUID
    purpose: str
    kind: str


@dataclass(frozen=True, slots=True)
class AssetProcessed:
    asset_id: uuid.UUID
    owner_id: uuid.UUID
    purpose: str
    kind: str


@dataclass(frozen=True, slots=True)
class AssetRejected:
    asset_id: uuid.UUID
    owner_id: uuid.UUID
    reason: str


@dataclass(frozen=True, slots=True)
class AssetDeleted:
    asset_id: uuid.UUID
    owner_id: uuid.UUID


type MediaEvent = AssetUploaded | AssetProcessed | AssetRejected | AssetDeleted


def record(outbox: Outbox, event: MediaEvent) -> None:
    """Кладёт событие в outbox текущей транзакции; ключ партиции это ресурс."""
    payload = {
        "asset_id": str(event.asset_id),
        "owner_id": str(event.owner_id),
    }
    if isinstance(event, AssetUploaded | AssetProcessed):
        payload.update(purpose=event.purpose, kind=event.kind)
    if isinstance(event, AssetRejected):
        payload["reason"] = event.reason
    outbox.add(
        topic=TOPIC_MEDIA,
        key=str(event.asset_id),
        event_type=type(event).__name__,
        payload=payload,
    )

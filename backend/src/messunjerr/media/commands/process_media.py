"""Заглушка обработки `process_media` (S5-06): тип файла по сигнатуре, статусы `processing` → `ready` или `rejected`.

Настоящая обработка изображений (перекодирование в WebP, EXIF, варианты, лимит мегапикселей, защита
от «бомб») приходит в S6 вместо `judge`; конвейер статусов, события и очистка остаются теми же.
Ресурсы, прошедшие заглушку, выглядят как `ready` без вариантов (`variants = {}`): S6 перегонит их.

Три шага, и ни один не держит блокировку БД во время сети:

1. короткая транзакция: ресурс `uploaded` (или зависший `processing`) становится `processing`;
2. чтение первых байт объекта из хранилища;
3. короткая транзакция: итог (`ready` либо `rejected` с причиной, событие в outbox); если за это время
   ресурс удалили, итог не записывается.

Задача идемпотентна: повтор после сбоя или дубль постановки видят уже готовое состояние и ничего не
делают. Сбой хранилища поднимается наверх (`StorageUnavailableError`), повторы решает обёртка задачи в
`messunjerr.jobs`. Недоступное хранилище не повод отклонять файл: ресурс остаётся в `processing`, его
подбирает `reconcile_uploads`, а если хранилище не вернётся за сутки, очистка удалит загрузку
(`cleanup_pending_uploads`). `processing_failed` ставится только когда объекта в хранилище действительно нет.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion
from messunjerr.media.domain.events import AssetProcessed, AssetRejected, record
from messunjerr.media.domain.ports import ObjectStorage
from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status
from messunjerr.media.domain.sniff import HEAD_BYTES, Verdict, judge
from messunjerr.media.infra.repositories import AssetRepository


class Outcome(StrEnum):
    READY = "ready"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    """Ресурса нет, его удалили или он уже обработан: делать нечего."""


@dataclass(frozen=True, slots=True)
class ProcessMedia:
    asset_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Claim:
    object_key: str
    kind: Kind
    purpose: Purpose


async def _claim(
    sessionmaker: async_sessionmaker[AsyncSession], asset_id: uuid.UUID
) -> _Claim | None:
    async with UnitOfWork(sessionmaker) as uow:
        row = await AssetRepository(uow.session).get(asset_id, for_update=True)
        if row is None or row.status not in (Status.UPLOADED, Status.PROCESSING):
            return None
        row.status = Status.PROCESSING
        claim = _Claim(row.object_key, Kind(row.kind), Purpose(row.purpose))
        await uow.commit()
        return claim


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    asset_id: uuid.UUID,
    verdict: Verdict,
    *,
    jobs: JobQueue,
    moment: datetime,
) -> Outcome:
    async with UnitOfWork(sessionmaker) as uow:
        row = await AssetRepository(uow.session).get(asset_id, for_update=True)
        if row is None or row.status != Status.PROCESSING:
            return Outcome.SKIPPED
        row.processed_at = moment
        if verdict.accepted:
            row.status = Status.READY
            if verdict.content_type is not None:
                row.content_type = verdict.content_type  # тип по содержимому важнее заявленного
            record(uow.outbox, AssetProcessed(row.id, row.owner_id, row.purpose, row.kind))
            await uow.commit()
            return Outcome.READY
        reason = verdict.reject_reason or RejectReason.PROCESSING_FAILED
        row.status = Status.REJECTED
        row.reject_reason = reason
        record(uow.outbox, AssetRejected(row.id, row.owner_id, reason.value))
        uow.after_commit(partial(enqueue_object_deletion, jobs, row.id))
        await uow.commit()
        return Outcome.REJECTED


async def process_media(
    command: ProcessMedia,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    jobs: JobQueue,
    now: datetime | None = None,
) -> Outcome:
    log = get_logger("messunjerr.media")
    claim = await _claim(sessionmaker, command.asset_id)
    if claim is None:
        return Outcome.SKIPPED

    head = await storage.read_head(claim.object_key, HEAD_BYTES)
    if head is None:  # объект пропал между завершением загрузки и обработкой
        verdict = Verdict(accepted=False, reject_reason=RejectReason.PROCESSING_FAILED)
    else:
        verdict = judge(claim.kind, claim.purpose, head)
    outcome = await _finish(
        sessionmaker, command.asset_id, verdict, jobs=jobs, moment=now or utcnow()
    )
    log.info(
        "media_processed",
        asset_id=str(command.asset_id),
        outcome=outcome.value,
        reason=verdict.reject_reason.value if verdict.reject_reason else None,
    )
    return outcome

"""Очистка и сверка медиа (S5-06, S5-07): фоновые задачи, которые запускает воркер.

- `cleanup_pending_uploads`: застрявшие загрузки старше суток (4.11) получают статус `deleted`
  (`pending` считаются от заявки, `uploaded` и `processing` от завершения загрузки); объекты потом
  убирает `delete_media_objects`. Пачками с `SKIP LOCKED`: несколько воркеров не мешают друг другу,
  долгих транзакций нет;
- `delete_media_objects`: удаляет объекты ресурсов `deleted` и `rejected` и ставит отметку
  `objects_deleted_at` (когда ссылка на загрузку уже не действует: до этого клиент мог бы положить
  объект заново). Удаление в хранилище идемпотентно, повтор безопасен;
- `reconcile_uploads`: временная замена Kafka (до S9–S13). Ставит `process_media` для загрузок, до
  которых обработка не дошла (задача потерялась при сбое Redis или воркер умер посреди работы), и
  `delete_media_objects` для тех, чьи объекты остались в хранилище. Задачи с детерминированным
  `job_id`, поэтому лишняя постановка дубля не создаёт;
- `sweep_orphan_objects`: раз в сутки сверяет объекты `uploads/` с таблицей. Объект без строки или со
  строкой `deleted` и `rejected`, объекты которой уже закрыты отметкой, это сирота (например, `PUT`
  с медленным телом дописался уже после очистки ресурса, а удаление аккаунта убрало строки
  каскадом). За один запуск удаляется не больше `MAX_REMOVALS` объектов: ошибка в настройках (БД не
  та, что у хранилища) не должна стереть всё разом.

Неприкреплённые `ready` старше 48 часов (4.11) здесь не чистятся: привязок (посты, сообщения) ещё нет,
знание «прикреплён ли ресурс» появится вместе с ними (S6, S11, S14).
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion, enqueue_processing
from messunjerr.media.domain.events import AssetDeleted, record
from messunjerr.media.domain.ports import ObjectStorage
from messunjerr.media.domain.rules import UPLOADS_PREFIX, Status
from messunjerr.media.infra.repositories import AssetRepository, AssetState

RECONCILE_GRACE = timedelta(minutes=2)
"""Сколько ресурсу дают дойти до итога самому, прежде чем сверка поставит задачу заново."""

LINK_LIFETIME_MARGIN = timedelta(seconds=60)
"""Запас сверх срока ссылки на загрузку: часы приложения и хранилища могут расходиться."""

ORPHAN_MIN_AGE = timedelta(hours=1)
"""Моложе объект сиротой не считается: его могла только что записать загрузка, которую ещё не видно."""

MAX_REMOVALS = 500
"""Сколько сирот сверка удаляет за один запуск (защита от массового удаления при ошибке настройки)."""

_UPLOAD_KEY = re.compile(
    r"^uploads/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
)


async def _enqueue_safely(jobs: JobQueue, asset_ids: Sequence[uuid.UUID]) -> None:
    """Ставит удаление объектов; сбой очереди не должен ронять очистку, подберёт сверка."""
    log = get_logger("messunjerr.media")
    for asset_id in asset_ids:
        try:
            await enqueue_object_deletion(jobs, asset_id)
        except Exception:
            log.warning("media_enqueue_failed", task="delete_media_objects", exc_info=True)
            return


async def cleanup_pending_uploads(
    sessionmaker: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    *,
    ttl: timedelta,
    now: datetime | None = None,
    batch_size: int = 200,
) -> int:
    """Помечает удалёнными загрузки, застрявшие дольше `ttl`; возвращает, сколько ресурсов."""
    moment = now or utcnow()
    cutoff = moment - ttl
    total = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            rows = await AssetRepository(uow.session).stale_uploads(before=cutoff, limit=batch_size)
            for row in rows:
                row.status = Status.DELETED
                row.deleted_at = moment
                record(uow.outbox, AssetDeleted(row.id, row.owner_id))
            ids = [row.id for row in rows]
            await uow.commit()
        await _enqueue_safely(jobs, ids)
        total += len(ids)
        if len(ids) < batch_size:
            get_logger("messunjerr.media").info("cleanup_pending_uploads", removed=total)
            return total


async def delete_media_objects(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    asset_ids: Sequence[uuid.UUID],
    *,
    link_lifetime: timedelta,
    now: datetime | None = None,
) -> int:
    """Убирает из хранилища объекты ресурсов `asset_ids` (только `deleted` и `rejected`).

    Ссылка на загрузку живёт `link_lifetime` с момента заявки. Пока она жива, клиент может положить
    объект заново после удаления ресурса или отказа (условная запись `If-None-Match` защищает от
    перезаписи, но не от создания на пустом месте, и такой объект не учтён ни в какой квоте).
    Поэтому объекты убираются сразу, а отметку `objects_deleted_at` строка получает лишь тогда, когда
    ссылка заведомо протухла; до этого сверка ставит удаление ещё раз.

    Возвращает число обработанных ресурсов. Сбой хранилища поднимается наверх: строки остаются
    открытыми, задача повторится, объекты не потеряются.

    Обращение к хранилищу идёт между двумя короткими транзакциями: соединение с БД не занято, пока
    хранилище отвечает (или не отвечает) секундами. Удаление идемпотентно, поэтому две одновременные
    задачи по одной строке лишь дважды удалят то же самое.
    """
    if not asset_ids:
        return 0
    moment = now or utcnow()
    async with UnitOfWork(sessionmaker) as uow:
        rows = await AssetRepository(uow.session).objects_to_delete(asset_ids, lock=False)
        pending = [row.id for row in rows]
        keys: list[str] = []
        for row in rows:
            keys.append(row.object_key)
            keys.extend(str(key) for key in row.variants.values())
    if not pending:
        return 0
    await storage.delete_many(keys)
    async with UnitOfWork(sessionmaker) as uow:
        for row in await AssetRepository(uow.session).objects_to_delete(pending):
            if moment >= row.created_at + link_lifetime:
                row.objects_deleted_at = moment
        await uow.commit()
    return len(pending)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    processing_requeued: int
    deletions_requeued: int


async def reconcile_uploads(
    sessionmaker: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    *,
    now: datetime | None = None,
    grace: timedelta = RECONCILE_GRACE,
    limit: int = 500,
) -> ReconcileResult:
    """Ставит заново потерянные задачи обработки и удаления объектов (без дублей по `job_id`)."""
    moment = now or utcnow()
    async with UnitOfWork(sessionmaker) as uow:
        repository = AssetRepository(uow.session)
        to_process = await repository.unprocessed_ids(uploaded_before=moment - grace, limit=limit)
        to_delete = await repository.objects_pending_ids(changed_before=moment - grace, limit=limit)

    processing = 0
    for asset_id in to_process:
        if await enqueue_processing(jobs, asset_id):
            processing += 1
    deletions = 0
    for asset_id in to_delete:
        if await enqueue_object_deletion(jobs, asset_id):
            deletions += 1
    if processing or deletions:
        get_logger("messunjerr.media").info(
            "reconcile_uploads", processing_requeued=processing, deletions_requeued=deletions
        )
    return ReconcileResult(processing_requeued=processing, deletions_requeued=deletions)


def _is_orphan(state: AssetState | None) -> bool:
    """Объекту места нет: ресурса не существует или его объекты уже были убраны и закрыты отметкой."""
    if state is None:
        return True
    return (
        state.status in (Status.DELETED, Status.REJECTED) and state.objects_deleted_at is not None
    )


@dataclass(frozen=True, slots=True)
class SweepResult:
    scanned: int
    orphans_found: int
    removed: int


async def sweep_orphan_objects(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    *,
    older_than: timedelta = ORPHAN_MIN_AGE,
    max_removals: int = MAX_REMOVALS,
    prefix: str = UPLOADS_PREFIX,
    now: datetime | None = None,
) -> SweepResult:
    """Удаляет объекты `uploads/`, которым не соответствует ни один живой ресурс.

    Хранилище читается страницами; к БД идёт по короткому запросу на страницу, так что соединение не
    занято, пока хранилище отвечает. Ключи не по шаблону `uploads/{id}/…` не трогаются.
    """
    cutoff = (now or utcnow()) - older_than
    scanned = found = removed = 0
    async for page in storage.list_objects(prefix):
        scanned += len(page)
        by_asset: dict[uuid.UUID, list[str]] = {}
        for item in page:
            match = _UPLOAD_KEY.match(item.key)
            if match is not None and item.modified_at < cutoff:
                by_asset.setdefault(uuid.UUID(match.group(1)), []).append(item.key)
        if not by_asset:
            continue
        async with UnitOfWork(sessionmaker) as uow:
            states = await AssetRepository(uow.session).states(list(by_asset))
        orphans = [
            key
            for asset_id, keys in by_asset.items()
            if _is_orphan(states.get(asset_id))
            for key in keys
        ]
        found += len(orphans)
        allowed = orphans[: max(0, max_removals - removed)]
        if allowed:
            await storage.delete_many(allowed)
            removed += len(allowed)
    log = get_logger("messunjerr.media")
    if found > removed:
        log.warning("sweep_orphan_objects_capped", found=found, removed=removed, cap=max_removals)
    if found:
        log.info("sweep_orphan_objects", scanned=scanned, found=found, removed=removed)
    return SweepResult(scanned=scanned, orphans_found=found, removed=removed)

"""Команда `DELETE /media/{asset_id}` (5.8, S5-05): удалить свой ресурс, пока он ни к чему не привязан.

Строка получает статус `deleted` сразу, а объекты убирает задача `delete_media_objects`, которую ставит
этот же вызов после коммита (если постановка потерялась, её подберёт `reconcile_uploads`). Повторное
удаление и чужой ресурс неотличимо отвечают `404`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import NotFoundError
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion
from messunjerr.media.domain import errors
from messunjerr.media.domain.events import AssetDeleted, record
from messunjerr.media.domain.ports import AssetUsage
from messunjerr.media.domain.rules import Status
from messunjerr.media.infra.repositories import AssetRepository


@dataclass(frozen=True, slots=True)
class DeleteAsset:
    owner_id: uuid.UUID
    asset_id: uuid.UUID


async def delete_asset(
    command: DeleteAsset,
    *,
    uow: UnitOfWork,
    usage: AssetUsage,
    jobs: JobQueue,
    now: datetime | None = None,
) -> None:
    moment = now or utcnow()
    row = await AssetRepository(uow.session).get(
        command.asset_id, owner_id=command.owner_id, for_update=True
    )
    if row is None:
        raise NotFoundError("The asset does not exist or is not yours.")
    if await usage.is_attached(uow.session, row.id):
        raise errors.asset_in_use()

    row.status = Status.DELETED
    row.deleted_at = moment
    record(uow.outbox, AssetDeleted(row.id, row.owner_id))
    uow.after_commit(partial(enqueue_object_deletion, jobs, row.id))
    await uow.commit()
    get_logger("messunjerr.media").info("asset_deleted", asset_id=str(row.id))

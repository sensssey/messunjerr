"""Команда `POST /media/uploads/{asset_id}/complete` (5.8, S5-05): проверить объект и передать на обработку.

Сервер проверяет объект запросом `HEAD` (он на месте, размер равен заявленному) и переводит ресурс в
`uploaded`; задачу `process_media` ставит после коммита. Повторный вызов идемпотентен: ресурс уже не
`pending`, поэтому возвращается его текущее состояние (у готового со ссылками). Запрос к хранилищу идёт между двумя
транзакциями: на это время соединение возвращается в пул и строка не заблокирована, поэтому
зависшее хранилище не вытесняет из пула остальные запросы.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import NotFoundError, ServiceUnavailableError
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion, enqueue_processing
from messunjerr.media.domain import errors
from messunjerr.media.domain.events import AssetRejected, AssetUploaded, record
from messunjerr.media.domain.ports import ObjectStorage, StorageUnavailableError
from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status, max_bytes
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.infra.repositories import AssetRepository
from messunjerr.media.queries.models import Asset
from messunjerr.media.queries.presenter import AssetPresenter


@dataclass(frozen=True, slots=True)
class CompleteUpload:
    owner_id: uuid.UUID
    asset_id: uuid.UUID


def size_problem(row: AssetRow, actual: int) -> RejectReason | None:
    """Почему размер объекта не годится: больше предела назначения или не равен заявленному."""
    if actual > max_bytes(Purpose(row.purpose), Kind(row.kind)):
        return RejectReason.SIZE_EXCEEDS_LIMIT
    if actual != row.declared_size:
        return RejectReason.SIZE_MISMATCH
    return None


async def complete_upload(
    command: CompleteUpload,
    *,
    uow: UnitOfWork,
    storage: ObjectStorage,
    jobs: JobQueue,
    presenter: AssetPresenter,
    now: datetime | None = None,
) -> Asset:
    moment = now or utcnow()
    repository = AssetRepository(uow.session)
    row = await repository.get(command.asset_id, owner_id=command.owner_id)
    if row is None:
        raise NotFoundError("The asset does not exist or is not yours.")
    if row.status != Status.PENDING:
        return await presenter.card(row)  # повтор: состояние уже продвинулось, отдаём как есть
    object_key = row.object_key
    await uow.rollback()  # отпускаем соединение: хранилище может отвечать секундами

    try:
        stored = await storage.head(object_key)
    except StorageUnavailableError as error:
        raise ServiceUnavailableError("The file storage is temporarily unavailable.", 5) from error
    if stored is None:
        raise errors.upload_missing()

    # Проверка статуса под блокировкой: параллельный `complete` или удаление могли успеть раньше.
    row = await repository.get(command.asset_id, owner_id=command.owner_id, for_update=True)
    if row is None:
        raise NotFoundError("The asset does not exist or is not yours.")
    if row.status != Status.PENDING:
        return await presenter.card(row)

    row.size_bytes = stored.size
    reason = size_problem(row, stored.size)
    if reason is not None:
        row.status = Status.REJECTED
        row.reject_reason = reason
        row.processed_at = moment
        record(uow.outbox, AssetRejected(row.id, row.owner_id, reason.value))
        uow.after_commit(partial(enqueue_object_deletion, jobs, row.id))
        await uow.commit()
        get_logger("messunjerr.media").info(
            "upload_rejected", asset_id=str(row.id), reason=reason.value
        )
        raise errors.upload_rejected(reason)

    row.status = Status.UPLOADED
    row.uploaded_at = moment
    record(uow.outbox, AssetUploaded(row.id, row.owner_id, row.purpose, row.kind))
    uow.after_commit(partial(enqueue_processing, jobs, row.id))
    await uow.commit()
    get_logger("messunjerr.media").info("upload_completed", asset_id=str(row.id))
    return await presenter.card(row)

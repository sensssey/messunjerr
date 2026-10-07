"""Репозиторий ресурсов: строки `media.assets`, квота владельца, выборки для плановых задач.

Репозиторий `commit()` не вызывает (4.3). Выборки для задач берут строки с `FOR UPDATE SKIP LOCKED`:
несколько воркеров разбирают разные строки и не ждут друг друга.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.media.domain.rules import IN_FLIGHT_STATUSES, Status
from messunjerr.media.infra.models import AssetRow

COUNTED_STATUSES = (*IN_FLIGHT_STATUSES, Status.READY)
"""Ресурсы, занимающие квоту: готовые и ещё идущие (с резервом заявленного размера)."""


@dataclass(frozen=True, slots=True)
class Usage:
    used_bytes: int
    assets_count: int


@dataclass(frozen=True, slots=True)
class AssetState:
    status: Status
    objects_deleted_at: datetime | None


class AssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: AssetRow) -> None:
        self._session.add(row)

    async def get(
        self,
        asset_id: uuid.UUID,
        *,
        owner_id: uuid.UUID | None = None,
        for_update: bool = False,
    ) -> AssetRow | None:
        """Ресурс по идентификатору; чужой (если задан владелец) и удалённый для вызывающего не существуют.

        `for_update` блокирует строку и перечитывает её из БД, даже если сессия уже держит её в памяти
        (иначе повторная проверка статуса увидела бы устаревшее значение).
        """
        statement = select(AssetRow).where(AssetRow.id == asset_id)
        if owner_id is not None:
            statement = statement.where(AssetRow.owner_id == owner_id)
        statement = statement.where(AssetRow.status != Status.DELETED)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def lock_quota(self, owner_id: uuid.UUID) -> None:
        """Сериализует проверки квоты одного владельца до конца транзакции.

        Без блокировки две одновременные заявки видели бы одно и то же свободное место и обе проходили
        бы. Ключ это хвост UUID (его случайная часть); чужие владельцы блокировку не замечают, редкое
        совпадение ключей лишь немного замедлит.
        """
        key = int.from_bytes(owner_id.bytes[8:], "big", signed=True)
        await self._session.execute(select(func.pg_advisory_xact_lock(key)))

    async def usage(self, owner_id: uuid.UUID) -> Usage:
        """Занятое место: размер готовых ресурсов и заявленный размер тех, что ещё загружаются."""
        size = case(
            (
                AssetRow.status == Status.READY,
                func.coalesce(AssetRow.size_bytes, AssetRow.declared_size),
            ),
            else_=AssetRow.declared_size,
        )
        statement = select(func.coalesce(func.sum(size), 0), func.count()).where(
            AssetRow.owner_id == owner_id, AssetRow.status.in_(COUNTED_STATUSES)
        )
        used, count = (await self._session.execute(statement)).one()
        return Usage(used_bytes=int(used), assets_count=int(count))

    # --- выборки для плановых задач
    async def states(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AssetState]:
        """Состояния ресурсов `ids` (включая удалённые); отсутствующих в ответе нет."""
        statement = select(AssetRow.id, AssetRow.status, AssetRow.objects_deleted_at).where(
            AssetRow.id.in_(ids)
        )
        return {
            row.id: AssetState(Status(row.status), row.objects_deleted_at)
            for row in (await self._session.execute(statement)).all()
        }

    async def stale_uploads(self, *, before: datetime, limit: int) -> Sequence[AssetRow]:
        """Загрузки, застрявшие дольше срока: `pending` по времени заявки, `uploaded` и `processing`
        по времени завершения загрузки (иначе файл, который ждёт упавший воркер, пропал бы уже
        через сутки после заявки, а не после завершения)."""
        statement = (
            select(AssetRow)
            .where(
                or_(
                    and_(AssetRow.status == Status.PENDING, AssetRow.created_at < before),
                    and_(
                        AssetRow.status.in_((Status.UPLOADED, Status.PROCESSING)),
                        func.coalesce(AssetRow.uploaded_at, AssetRow.created_at) < before,
                    ),
                )
            )
            .order_by(AssetRow.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def unprocessed_ids(
        self, *, uploaded_before: datetime, limit: int
    ) -> Sequence[uuid.UUID]:
        """Ресурсы, которые загружены давно, а до `ready` или `rejected` так и не дошли."""
        statement = (
            select(AssetRow.id)
            .where(
                AssetRow.status.in_((Status.UPLOADED, Status.PROCESSING)),
                AssetRow.uploaded_at < uploaded_before,
            )
            .order_by(AssetRow.uploaded_at)
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def objects_pending_ids(
        self, *, changed_before: datetime, limit: int
    ) -> Sequence[uuid.UUID]:
        """Удалённые и отклонённые ресурсы, чьи объекты ещё лежат в хранилище."""
        statement = (
            select(AssetRow.id)
            .where(
                AssetRow.status.in_((Status.DELETED, Status.REJECTED)),
                AssetRow.objects_deleted_at.is_(None),
                func.coalesce(AssetRow.deleted_at, AssetRow.processed_at, AssetRow.created_at)
                < changed_before,
            )
            .order_by(AssetRow.created_at)
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def objects_to_delete(
        self, ids: Sequence[uuid.UUID], *, lock: bool = True
    ) -> Sequence[AssetRow]:
        """Строки `ids`, объекты которых пора удалять.

        `lock` берёт строки с `SKIP LOCKED`: их закрывает та задача, что взяла блокировку.
        """
        statement = select(AssetRow).where(
            AssetRow.id.in_(ids),
            AssetRow.status.in_((Status.DELETED, Status.REJECTED)),
            AssetRow.objects_deleted_at.is_(None),
        )
        if lock:
            statement = statement.with_for_update(skip_locked=True)
        return (await self._session.execute(statement)).scalars().all()

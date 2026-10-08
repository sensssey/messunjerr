"""Аватар профиля поверх ресурсов медиа (S6-04): порт `AvatarAssets`, который объявили профили.

Профили стоят ниже медиа в графе контекстов (4.2) и ничего из него не импортируют, а медиа свободно
импортирует профили. Поэтому порт описан в `profiles.domain.ports`, а реализует его этот класс;
приложение собирает их вместе в `messunjerr.main`.
"""

import uuid
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion
from messunjerr.media.domain.events import AssetDeleted, record
from messunjerr.media.domain.rules import Purpose, Status
from messunjerr.media.infra.repositories import AssetRepository
from messunjerr.profiles.domain.ports import AvatarCheck


class MediaAvatarAssets:
    def __init__(self, jobs: JobQueue) -> None:
        self._jobs = jobs

    async def check(
        self, session: AsyncSession, *, owner_id: uuid.UUID, asset_id: uuid.UUID
    ) -> AvatarCheck:
        """Годится ли ресурс в аватары: свой, назначения `avatar`, обработан до конца.

        Чужой, удалённый и несуществующий отвечают одинаково (`asset_not_found`): чужие идентификаторы
        перебором не проверить. Строка блокируется: параллельное удаление ресурса (`DELETE /media/{id}`)
        дождётся конца этой транзакции, увидит привязку и ответит `asset_in_use`, а не оставит
        профиль с удалённым аватаром.
        """
        row = await AssetRepository(session).get(asset_id, owner_id=owner_id, for_update=True)
        if row is None:
            return AvatarCheck.NOT_FOUND
        if row.purpose != Purpose.AVATAR:
            return AvatarCheck.WRONG_PURPOSE
        # Без вариантов (готовые ресурсы времён S5 ждут перерасчёта) публичных файлов ещё нет.
        if row.status != Status.READY or not row.variants:
            return AvatarCheck.NOT_READY
        return AvatarCheck.OK

    async def release(self, uow: UnitOfWork, *, owner_id: uuid.UUID, asset_id: uuid.UUID) -> None:
        """Заменённый или убранный аватар удаляется: иначе он занимал бы квоту и лежал в публичном
        префиксе навсегда. Адрес прежнего аватара неизменяем и мог остаться в кэше браузеров, но
        новые ответы API его уже не называют."""
        row = await AssetRepository(uow.session).get(asset_id, owner_id=owner_id, for_update=True)
        if row is None:
            return
        row.status = Status.DELETED
        row.deleted_at = utcnow()
        record(uow.outbox, AssetDeleted(row.id, row.owner_id))
        uow.after_commit(partial(enqueue_object_deletion, self._jobs, row.id))
        get_logger("messunjerr.media").info("avatar_released", asset_id=str(row.id))

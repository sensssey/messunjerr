"""Перегон готовых изображений времён S5 через настоящую обработку (S6, `messunjerr reprocess-media`).

В S5 обработка была заглушкой: она проверяла сигнатуру и ничего не перекодировала, поэтому у готовых
изображений нет вариантов (`variants = {}`), а оригиналы с EXIF лежат в хранилище как загружены.
Команда возвращает такие ресурсы в `uploaded` и ставит им `process_media`; обычная обработка делает
варианты и заменяет оригинал пустым объектом. Событий в outbox команда не пишет: для остальных
контекстов ресурс не менялся.

До первой выкладки на боевой сервер таких ресурсов нет (S5 жил только на локальных стендах), так что
это инструмент для локальных баз; повтор безопасен: обработанные ресурсы под условие не подходят.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_processing
from messunjerr.media.domain.rules import Kind, Status
from messunjerr.media.infra.models import AssetRow


async def _legacy_ready_images(session: AsyncSession, limit: int) -> Sequence[AssetRow]:
    statement = (
        select(AssetRow)
        .where(
            AssetRow.status == Status.READY,
            AssetRow.kind == Kind.IMAGE,
            AssetRow.variants == {},
        )
        .order_by(AssetRow.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return (await session.execute(statement)).scalars().all()


async def reprocess_legacy_images(
    sessionmaker: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    *,
    now: datetime | None = None,
    batch_size: int = 200,
) -> int:
    """Возвращает на обработку готовые изображения без вариантов; сколько ресурсов вернули."""
    moment = now or utcnow()
    total = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            rows = await _legacy_ready_images(uow.session, batch_size)
            for row in rows:
                row.status = Status.UPLOADED
                row.processed_at = None
                # Иначе суточная очистка застрявших загрузок сочла бы ресурс давно брошенным.
                row.uploaded_at = moment
            ids = [row.id for row in rows]
            await uow.commit()
        for asset_id in ids:
            await enqueue_processing(jobs, asset_id)
        total += len(ids)
        if len(ids) < batch_size:
            break
    if total:
        get_logger("messunjerr.media").info("reprocess_legacy_images", count=total)
    return total

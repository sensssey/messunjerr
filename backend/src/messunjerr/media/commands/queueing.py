"""Постановка задач медиа в очередь: детерминированные `job_id`, чтобы повтор не создавал дубль (4.12).

Постановка идёт после коммита и может не удаться (Redis недоступен): состояние уже в БД, а потерянные
постановки подбирает `reconcile_uploads` (временная схема до Kafka, план спринтов S5-06).
"""

import uuid

from messunjerr.core.jobs import (
    QUEUE_MEDIA,
    TASK_DELETE_MEDIA_OBJECTS,
    TASK_PROCESS_MEDIA,
    JobQueue,
)


async def enqueue_processing(jobs: JobQueue, asset_id: uuid.UUID) -> bool:
    """Ставит `process_media`; `False`, если такая задача уже есть."""
    return await jobs.enqueue(
        TASK_PROCESS_MEDIA,
        queue=QUEUE_MEDIA,
        job_id=f"{TASK_PROCESS_MEDIA}:{asset_id}",
        asset_id=str(asset_id),
    )


async def enqueue_object_deletion(jobs: JobQueue, asset_id: uuid.UUID) -> bool:
    """Ставит `delete_media_objects` для одного ресурса; `False`, если задача уже есть."""
    return await jobs.enqueue(
        TASK_DELETE_MEDIA_OBJECTS,
        queue=QUEUE_MEDIA,
        job_id=f"{TASK_DELETE_MEDIA_OBJECTS}:{asset_id}",
        asset_ids=[str(asset_id)],
    )

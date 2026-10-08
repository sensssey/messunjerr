"""Фоновые задачи медиа (S5-06, S5-07): обработка-заглушка, очистка, удаление объектов, сверка.

Команды работают на настоящей БД с подставными хранилищем и очередью. Отдельный сквозной тест гоняет
настоящий arq: API ставит `process_media` в Redis, воркер очереди `media` его разбирает.
"""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from arq.worker import Retry
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import QUEUE_MEDIA, InMemoryJobQueue
from messunjerr.jobs import tasks
from messunjerr.jobs.worker import build_worker, cron_jobs_for, functions_for
from messunjerr.main import create_app
from messunjerr.media.commands import housekeeping
from messunjerr.media.commands.process_media import Outcome, ProcessMedia, process_media
from messunjerr.media.domain.ports import StorageUnavailableError
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.settings import Settings

from .helpers import execute, fetch_all, fetch_one, verified_user
from .media_helpers import (
    GIF,
    JPEG,
    PDF,
    ProbingStorage,
    key_of,
    put_object,
    started,
    uploaded,
)

Sessions = async_sessionmaker[AsyncSession]
NO_WINDOW = timedelta(0)
"""Ссылка на загрузку уже не действует: строка закрывается сразу после удаления объектов."""


async def row_of(engine: AsyncEngine, asset_id: str) -> dict[str, Any]:
    return await fetch_one(
        engine, "SELECT * FROM media.assets WHERE id = :id", id=uuid.UUID(asset_id)
    )


async def events_of(engine: AsyncEngine) -> list[str]:
    rows = await fetch_all(
        engine, "SELECT event_type FROM platform.outbox WHERE topic = 'mj.media.v1' ORDER BY id"
    )
    return [row["event_type"] for row in rows]


async def age(engine: AsyncEngine, asset_id: str, **interval: int) -> None:
    """Состарить ресурс: создан, загружен и изменён `interval` назад."""
    [(unit, amount)] = interval.items()
    await execute(
        engine,
        f"UPDATE media.assets SET created_at = now() - make_interval({unit} => :n), "
        f"uploaded_at = CASE WHEN uploaded_at IS NULL THEN NULL ELSE now() - make_interval({unit} => :n) END "
        "WHERE id = :id",
        n=amount,
        id=uuid.UUID(asset_id),
    )


async def run_processing(
    asset_id: str, sessions: Sessions, storage: InMemoryObjectStorage, jobs: InMemoryJobQueue
) -> Outcome:
    return await process_media(
        ProcessMedia(uuid.UUID(asset_id)), sessionmaker=sessions, storage=storage, jobs=jobs
    )


# ----------------------------------------------------------------------------- process_media
# Обработка изображений и файлов, отказы и ловушки проверяет test_media_processing.py; здесь
# то, что касается самих задач: пропуск лишнего, повтор, гонка с удалением, обёртка arq.
async def test_processing_does_nothing_for_assets_that_are_not_waiting_for_it(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    pending = await started(client, user)  # загрузка не завершена
    done = await uploaded(client, user, storage)
    assert await run_processing(done["asset"]["id"], sessionmaker, storage, jobs) is Outcome.READY
    removed = await uploaded(client, user, storage)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted' WHERE id = :id",
        id=uuid.UUID(removed["asset"]["id"]),
    )

    assert (
        await run_processing(pending["asset"]["id"], sessionmaker, storage, jobs) is Outcome.SKIPPED
    )
    assert (
        await run_processing(done["asset"]["id"], sessionmaker, storage, jobs) is Outcome.SKIPPED
    )  # повтор
    assert (
        await run_processing(removed["asset"]["id"], sessionmaker, storage, jobs) is Outcome.SKIPPED
    )
    assert await run_processing(str(uuid.uuid4()), sessionmaker, storage, jobs) is Outcome.SKIPPED
    assert (await row_of(admin_engine, pending["asset"]["id"]))["status"] == "pending"
    assert (await row_of(admin_engine, removed["asset"]["id"]))["status"] == "deleted"


async def test_a_job_interrupted_midway_is_finished_by_the_next_run(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'processing' WHERE id = :id",
        id=uuid.UUID(asset_id),
    )

    assert await run_processing(asset_id, sessionmaker, storage, jobs) is Outcome.READY


class DeletesWhileReading(InMemoryObjectStorage):
    """Пока воркер читает объект, владелец успевает удалить ресурс."""

    def __init__(self, engine: AsyncEngine, asset_id: str) -> None:
        super().__init__()
        self.engine, self.asset_id = engine, asset_id

    async def read_head(self, key: str, length: int) -> bytes | None:
        await execute(
            self.engine,
            "UPDATE media.assets SET status = 'deleted' WHERE id = :id",
            id=uuid.UUID(self.asset_id),
        )
        return await super().read_head(key, length)


async def test_an_asset_deleted_during_processing_stays_deleted(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    racing = DeletesWhileReading(admin_engine, asset_id)
    racing.put(key_of(created), JPEG)

    outcome = await run_processing(asset_id, sessionmaker, racing, jobs)

    assert outcome is Outcome.SKIPPED
    assert (await row_of(admin_engine, asset_id))["status"] == "deleted"
    assert "AssetProcessed" not in await events_of(admin_engine)


# ----------------------------------------------------------------------------- обёртка задачи arq
def context_for(
    sessions: Sessions,
    storage: InMemoryObjectStorage,
    jobs: InMemoryJobQueue,
    settings: Settings,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "sessionmaker": sessions,
        "storage": storage,
        "jobs": jobs,
        "settings": settings,
        "job_try": attempt,
    }


async def test_the_task_retries_a_storage_outage_and_gives_up_after_the_last_attempt(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    storage.unavailable = True

    with pytest.raises(Retry) as retry:
        await tasks.process_media(
            context_for(sessionmaker, storage, jobs, test_settings, 1), asset_id=asset_id
        )
    assert retry.value.defer_score == 30_000  # пауза 30 с, растёт с попытками
    assert (await row_of(admin_engine, asset_id))["status"] == "processing"  # захвачен, итога нет

    with pytest.raises(Retry):
        await tasks.process_media(
            context_for(sessionmaker, storage, jobs, test_settings, 2), asset_id=asset_id
        )

    last = await tasks.process_media(
        context_for(sessionmaker, storage, jobs, test_settings, tasks.PROCESS_MEDIA_MAX_TRIES),
        asset_id=asset_id,
    )
    assert last == "deferred"
    row = await row_of(admin_engine, asset_id)
    # Недоступное хранилище не повод отклонять и стирать файл: он ждёт сверку.
    assert (row["status"], row["reject_reason"]) == ("processing", None)
    assert key_of(created) in storage.objects
    assert "AssetRejected" not in await events_of(admin_engine)


async def test_a_long_storage_outage_costs_nobody_their_files(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    storage.unavailable = True
    for attempt in range(1, tasks.PROCESS_MEDIA_MAX_TRIES + 1):  # все попытки в простое хранилища
        try:
            await tasks.process_media(
                context_for(sessionmaker, storage, jobs, test_settings, attempt), asset_id=asset_id
            )
        except Retry:
            continue
    assert (await row_of(admin_engine, asset_id))["status"] == "processing"
    jobs.clear()

    # Хранилище вернулось: сверка подбирает застрявший ресурс, и он проходит обработку.
    result = await housekeeping.reconcile_uploads(
        sessionmaker, jobs, now=utcnow() + timedelta(minutes=10)
    )
    storage.unavailable = False
    assert result.processing_requeued == 1
    assert await run_processing(asset_id, sessionmaker, storage, jobs) is Outcome.READY


async def test_the_task_succeeds_after_the_storage_comes_back(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    storage.unavailable = True
    with pytest.raises(Retry):
        await tasks.process_media(
            context_for(sessionmaker, storage, jobs, test_settings), asset_id=asset_id
        )
    storage.unavailable = False

    result = await tasks.process_media(
        context_for(sessionmaker, storage, jobs, test_settings, 2), asset_id=asset_id
    )

    assert result == "ready"


# ----------------------------------------------------------------------------- cleanup_pending_uploads
async def test_stale_uploads_are_deleted_and_everything_else_stays(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    stale_pending = await started(client, user)
    stale_uploaded = await uploaded(client, user, storage)
    stale_ready = await uploaded(client, user, storage)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(stale_ready["asset"]["id"]),
    )
    fresh = await started(client, user)
    for created in (stale_pending, stale_uploaded, stale_ready):
        await age(admin_engine, created["asset"]["id"], hours=25)
    jobs.clear()

    removed = await housekeeping.cleanup_pending_uploads(
        sessionmaker, jobs, ttl=timedelta(hours=24)
    )

    assert removed == 2
    statuses = {
        name: (await row_of(admin_engine, created["asset"]["id"]))["status"]
        for name, created in {
            "stale_pending": stale_pending,
            "stale_uploaded": stale_uploaded,
            "stale_ready": stale_ready,
            "fresh": fresh,
        }.items()
    }
    assert statuses == {
        "stale_pending": "deleted",
        "stale_uploaded": "deleted",
        "stale_ready": "ready",  # готовые чистятся вместе с привязками (S6), не здесь
        "fresh": "pending",
    }
    assert (
        sorted(event for event in await events_of(admin_engine) if event == "AssetDeleted")
        == ["AssetDeleted"] * 2
    )
    queued = {job.kwargs["asset_ids"][0] for job in jobs.named("delete_media_objects")}
    assert queued == {stale_pending["asset"]["id"], stale_uploaded["asset"]["id"]}


async def test_a_completed_upload_waits_a_day_from_completion_not_from_the_request(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    waiting = await uploaded(client, user, storage)  # заявка давно, загрузка завершена недавно
    stuck = await uploaded(client, user, storage)  # обработка застряла больше суток
    for created, completed_hours_ago in ((waiting, 2), (stuck, 26)):
        await execute(
            admin_engine,
            "UPDATE media.assets SET created_at = now() - interval '30 hours', "
            "uploaded_at = now() - make_interval(hours => :h) WHERE id = :id",
            h=completed_hours_ago,
            id=uuid.UUID(created["asset"]["id"]),
        )
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'processing' WHERE id = :id",
        id=uuid.UUID(stuck["asset"]["id"]),
    )

    removed = await housekeeping.cleanup_pending_uploads(
        sessionmaker, jobs, ttl=timedelta(hours=24)
    )

    assert removed == 1
    assert (await row_of(admin_engine, waiting["asset"]["id"]))["status"] == "uploaded"
    assert (await row_of(admin_engine, stuck["asset"]["id"]))["status"] == "deleted"


async def test_cleanup_is_repeatable_and_works_in_small_batches(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    ids = [(await started(client, user))["asset"]["id"] for _ in range(5)]
    for asset_id in ids:
        await age(admin_engine, asset_id, hours=30)

    first = await housekeeping.cleanup_pending_uploads(
        sessionmaker, jobs, ttl=timedelta(hours=24), batch_size=2
    )
    second = await housekeeping.cleanup_pending_uploads(sessionmaker, jobs, ttl=timedelta(hours=24))

    assert (first, second) == (5, 0)


async def test_cleanup_survives_a_queue_outage_and_leaves_the_removal_to_reconcile(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user)
    await age(admin_engine, created["asset"]["id"], hours=30)
    jobs.clear()
    jobs.fail_with = RuntimeError("redis is down")

    removed = await housekeeping.cleanup_pending_uploads(
        sessionmaker, jobs, ttl=timedelta(hours=24)
    )

    assert removed == 1
    assert (await row_of(admin_engine, created["asset"]["id"]))["status"] == "deleted"
    jobs.fail_with = None
    result = await housekeeping.reconcile_uploads(
        sessionmaker, jobs, now=utcnow() + timedelta(minutes=10)
    )
    assert result.deletions_requeued == 1


# ----------------------------------------------------------------------------- delete_media_objects
@pytest.mark.parametrize(
    ("overrides", "extra"),
    [
        ({}, ["uploads/{id}/thumb.webp", "uploads/{id}/medium.webp"]),
        ({"purpose": "avatar"}, ["public/avatars/{id}/64.webp", "public/avatars/{id}/256.webp"]),
        (
            {"purpose": "message", "content_type": "application/pdf", "filename": "a.pdf"},
            [],
        ),
    ],
)
async def test_object_removal_deletes_the_original_and_all_variants_and_marks_the_row(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    overrides: dict[str, Any],
    extra: list[str],
) -> None:
    user = await verified_user(client, jobs)
    body = PDF if overrides.get("content_type") == "application/pdf" else JPEG
    created = await uploaded(client, user, storage, body, **overrides)
    asset_id = created["asset"]["id"]
    variant_keys = [pattern.format(id=asset_id) for pattern in extra]
    for key in variant_keys:
        storage.put(
            key, b"v"
        )  # варианты, которые записала обработка (в карточке их может и не быть)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted', deleted_at = now() WHERE id = :id",
        id=uuid.UUID(asset_id),
    )

    done = await housekeeping.delete_media_objects(
        sessionmaker, storage, [uuid.UUID(asset_id)], link_lifetime=NO_WINDOW
    )

    assert done == 1
    assert storage.objects == {}
    assert set(storage.deleted) == {key_of(created), *variant_keys}
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is not None
    # Повтор ничего не делает: объекты уже убраны.
    assert (
        await housekeeping.delete_media_objects(
            sessionmaker, storage, [uuid.UUID(asset_id)], link_lifetime=NO_WINDOW
        )
        == 0
    )


async def test_object_removal_never_touches_live_assets(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)

    done = await housekeeping.delete_media_objects(
        sessionmaker,
        storage,
        [uuid.UUID(created["asset"]["id"]), uuid.uuid4()],
        link_lifetime=NO_WINDOW,
    )

    assert done == 0
    assert key_of(created) in storage.objects


async def test_a_storage_outage_keeps_the_row_pending_so_the_next_run_repeats_the_removal(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await client.delete(f"/api/v1/media/{asset_id}", headers=user.headers)
    await age(admin_engine, asset_id, mins=30)  # ссылка на загрузку давно протухла
    storage.unavailable = True

    with pytest.raises(StorageUnavailableError):
        await housekeeping.delete_media_objects(
            sessionmaker, storage, [uuid.UUID(asset_id)], link_lifetime=NO_WINDOW
        )
    with pytest.raises(Retry):
        await tasks.delete_media_objects(
            context_for(sessionmaker, storage, jobs, test_settings), asset_ids=[asset_id]
        )
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is None

    storage.unavailable = False
    assert (
        await tasks.delete_media_objects(
            context_for(sessionmaker, storage, jobs, test_settings, 2), asset_ids=[asset_id]
        )
        == 1
    )
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is not None
    assert key_of(created) not in storage.objects


async def test_objects_are_removed_again_while_the_upload_link_is_still_alive(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    """Условная запись не мешает создать объект заново после удаления: проходим по нему ещё раз."""
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = uuid.UUID(created["asset"]["id"])
    await client.delete(f"/api/v1/media/{asset_id}", headers=user.headers)
    window = timedelta(minutes=16)

    first = await housekeeping.delete_media_objects(
        sessionmaker, storage, [asset_id], link_lifetime=window
    )

    assert first == 1
    assert key_of(created) not in storage.objects
    assert (await row_of(admin_engine, str(asset_id)))["objects_deleted_at"] is None

    storage.put(key_of(created), b"late upload through the live link")
    inside = await housekeeping.delete_media_objects(
        sessionmaker, storage, [asset_id], link_lifetime=window, now=utcnow() + timedelta(minutes=5)
    )
    assert inside == 1
    assert key_of(created) not in storage.objects
    assert (await row_of(admin_engine, str(asset_id)))["objects_deleted_at"] is None

    storage.put(key_of(created), b"and once more")
    after = await housekeeping.delete_media_objects(
        sessionmaker, storage, [asset_id], link_lifetime=window, now=utcnow() + window
    )
    assert after == 1
    assert key_of(created) not in storage.objects
    assert (await row_of(admin_engine, str(asset_id)))["objects_deleted_at"] is not None
    # Закрытую строку больше не трогаем.
    assert (
        await housekeeping.delete_media_objects(
            sessionmaker, storage, [asset_id], link_lifetime=window, now=utcnow() + window
        )
        == 0
    )


async def test_the_removal_task_keeps_the_row_open_for_the_lifetime_of_the_link_from_the_settings(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await client.delete(f"/api/v1/media/{asset_id}", headers=user.headers)
    context = context_for(sessionmaker, storage, jobs, test_settings)

    # Заявка только что создана: срок ссылки (UPLOAD_URL_TTL_SECONDS и минута запаса) ещё идёт.
    assert await tasks.delete_media_objects(context, asset_ids=[asset_id]) == 1
    assert key_of(created) not in storage.objects
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is None

    ttl = test_settings.upload_url_ttl_seconds
    await execute(
        admin_engine,
        "UPDATE media.assets SET created_at = now() - make_interval(secs => :age) WHERE id = :id",
        age=ttl + 30,  # ссылка протухла, а запас в минуту ещё нет
        id=uuid.UUID(asset_id),
    )
    assert await tasks.delete_media_objects(context, asset_ids=[asset_id]) == 1
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is None

    await execute(
        admin_engine,
        "UPDATE media.assets SET created_at = now() - make_interval(secs => :age) WHERE id = :id",
        age=ttl + 90,
        id=uuid.UUID(asset_id),
    )
    assert await tasks.delete_media_objects(context, asset_ids=[asset_id]) == 1
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is not None


# ----------------------------------------------------------------------------- reconcile_uploads
async def test_reconcile_requeues_lost_processing_and_lost_removals_once(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    lost = await uploaded(client, user, storage)  # process_media потерялась
    stuck = await uploaded(client, user, storage)
    just_now = await uploaded(client, user, storage)  # ещё в пределах льготного срока
    removed = await uploaded(client, user, storage)
    removed_done = await uploaded(client, user, storage)
    for created in (lost, stuck, removed, removed_done):
        await age(admin_engine, created["asset"]["id"], mins=30)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'processing' WHERE id = :id",
        id=uuid.UUID(stuck["asset"]["id"]),
    )
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted', deleted_at = now() - interval '30 minutes' WHERE id = :id",
        id=uuid.UUID(removed["asset"]["id"]),
    )
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted', deleted_at = now() - interval '30 minutes', "
        "objects_deleted_at = now() WHERE id = :id",
        id=uuid.UUID(removed_done["asset"]["id"]),
    )
    jobs.clear()

    first = await housekeeping.reconcile_uploads(sessionmaker, jobs)
    second = await housekeeping.reconcile_uploads(sessionmaker, jobs)

    assert (first.processing_requeued, first.deletions_requeued) == (2, 1)
    assert (second.processing_requeued, second.deletions_requeued) == (
        0,
        0,
    )  # job_id не даёт дублей
    assert {job.kwargs["asset_id"] for job in jobs.named("process_media")} == {
        lost["asset"]["id"],
        stuck["asset"]["id"],
    }
    assert [job.kwargs["asset_ids"] for job in jobs.named("delete_media_objects")] == [
        [removed["asset"]["id"]]
    ]
    assert just_now["asset"]["id"] not in {job.kwargs.get("asset_id") for job in jobs.jobs}


# ----------------------------------------------------------------------------- sweep_orphan_objects
def orphan_key() -> str:
    return f"uploads/{uuid.uuid4()}/original"


def old_enough(storage: InMemoryObjectStorage, *keys: str) -> None:
    for key in keys:
        storage.modified[key] = utcnow() - timedelta(hours=3)


async def test_the_sweep_removes_only_objects_without_a_living_asset(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    pending = await started(client, user)
    put_object(storage, pending, JPEG)  # файл уже на месте, complete ещё не вызван
    ready = await uploaded(client, user, storage)
    waiting = await uploaded(client, user, storage)
    window_open = await uploaded(client, user, storage)  # удалён, объекты ещё закрывает задача
    window_closed = await uploaded(client, user, storage)  # удалён, объекты убраны, а они снова тут
    rejected_closed = await uploaded(client, user, storage)
    for created, status, marked in (
        (ready, "ready", False),
        (window_open, "deleted", False),
        (window_closed, "deleted", True),
        (rejected_closed, "rejected", True),
    ):
        await execute(
            admin_engine,
            "UPDATE media.assets SET status = :status, "
            "objects_deleted_at = CASE WHEN :marked THEN now() END WHERE id = :id",
            status=status,
            marked=marked,
            id=uuid.UUID(created["asset"]["id"]),
        )
    await execute(
        admin_engine,
        "UPDATE media.assets SET variants = CAST(:variants AS jsonb) WHERE id = :id",
        variants='{"thumb": {}, "medium": {}}',  # обработан: варианты записаны
        id=uuid.UUID(ready["asset"]["id"]),
    )
    without_row, young = orphan_key(), orphan_key()
    storage.put(without_row, b"x")
    storage.put(young, b"x")
    storage.put("uploads/readme.txt", b"x")  # не по шаблону: не трогаем
    storage.put("public/avatars/a/64.webp", b"x")  # другой префикс
    keys = {
        "pending": key_of(pending),
        "ready": key_of(ready),
        "waiting": key_of(waiting),
        "window_open": key_of(window_open),
        "window_closed": key_of(window_closed),
        "rejected_closed": key_of(rejected_closed),
    }
    old_enough(storage, *keys.values(), without_row, "uploads/readme.txt")

    result = await housekeeping.sweep_orphan_objects(sessionmaker, storage)

    assert set(storage.deleted) == {keys["window_closed"], keys["rejected_closed"], without_row}
    assert (result.orphans_found, result.removed) == (3, 3)
    assert result.scanned == len(keys) + 4  # без объекта вне `uploads/` и `public/avatars/`
    survivors = set(storage.objects)
    assert {keys["pending"], keys["ready"], keys["waiting"], keys["window_open"]} <= survivors
    assert {young, "uploads/readme.txt", "public/avatars/a/64.webp"} <= survivors
    # Готовое изображение: его оригинал (с EXIF) сверка заменила пустым объектом, а не удалила.
    assert result.scrubbed == 1
    assert storage.objects[keys["ready"]][0] == b""
    assert storage.objects[keys["waiting"]][0] == JPEG  # ещё не обработан: оригинал нужен


async def set_state(
    engine: AsyncEngine,
    created: dict[str, Any],
    status: str,
    *,
    marked: bool = False,
    **columns: str,
) -> None:
    extra = "".join(f", {name} = CAST(:{name} AS jsonb)" for name in columns)
    await execute(
        engine,
        f"UPDATE media.assets SET status = :status, "
        f"objects_deleted_at = CASE WHEN :marked THEN now() END{extra} WHERE id = :id",
        status=status,
        marked=marked,
        id=uuid.UUID(created["asset"]["id"]),
        **columns,
    )


async def test_the_sweep_looks_after_the_public_avatar_variants_too(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    living = await uploaded(client, user, storage, JPEG, purpose="avatar")
    gone = await uploaded(client, user, storage, JPEG, purpose="avatar")
    await set_state(admin_engine, living, "ready", variants='{"thumb": {}, "medium": {}}')
    await set_state(admin_engine, gone, "deleted", marked=True)
    nameless = str(uuid.uuid4())
    keys = {
        "living": [
            f"public/avatars/{living['asset']['id']}/{name}" for name in ("64.webp", "256.webp")
        ],
        "gone": [
            f"public/avatars/{gone['asset']['id']}/{name}" for name in ("64.webp", "256.webp")
        ],
        "nameless": [f"public/avatars/{nameless}/64.webp"],
    }
    for key in [key for group in keys.values() for key in group]:
        storage.put(key, b"w")
    old_enough(storage, *[key for group in keys.values() for key in group])

    result = await housekeeping.sweep_orphan_objects(sessionmaker, storage)

    assert set(storage.deleted) >= {*keys["gone"], *keys["nameless"]}
    assert set(keys["living"]) <= set(storage.objects)  # живой аватар не трогаем
    assert not set(storage.deleted) & set(keys["living"])
    assert result.orphans_found == 3


async def test_the_sweep_finishes_the_scrub_of_originals_of_ready_images_only(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    photo = await uploaded(client, user, storage, JPEG)  # готов, а оригинал не заменили
    legacy = await uploaded(client, user, storage, JPEG)  # готов по заглушке S5: вариантов нет
    kept_gif = await uploaded(client, user, storage, GIF, content_type="image/gif")
    document = await uploaded(
        client,
        user,
        storage,
        PDF,
        purpose="message",
        content_type="application/pdf",
        filename="a.pdf",
    )
    already_empty = await uploaded(client, user, storage, JPEG)
    waiting = await uploaded(client, user, storage, JPEG)
    for created in (legacy, document):
        await set_state(admin_engine, created, "ready")
    for created in (photo, already_empty):
        await set_state(admin_engine, created, "ready", variants='{"thumb": {}, "medium": {}}')
    await set_state(
        admin_engine, kept_gif, "ready", variants='{"thumb": {}, "original": {"key": "x"}}'
    )
    storage.put(key_of(already_empty), b"")
    keys = [
        key_of(created) for created in (photo, legacy, kept_gif, document, already_empty, waiting)
    ]
    old_enough(storage, *keys)

    result = await housekeeping.sweep_orphan_objects(sessionmaker, storage)

    assert result.scrubbed == 1
    assert storage.writes == [key_of(photo)]
    assert storage.objects[key_of(photo)][0] == b""
    assert (
        storage.objects[key_of(legacy)][0] == JPEG
    )  # единственная копия: сначала `reprocess-media`
    assert storage.objects[key_of(kept_gif)][0] == GIF  # анимацию храним как есть
    assert storage.objects[key_of(document)][0] == PDF  # файл не трогаем
    assert storage.objects[key_of(waiting)][0] == JPEG  # ещё не обработан
    again = await housekeeping.sweep_orphan_objects(sessionmaker, storage)
    assert again.scrubbed == 0  # пустое второй раз не переписывается


async def test_the_scrub_is_capped_per_run_too(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = [await uploaded(client, user, storage, JPEG) for _ in range(3)]
    for item in created:
        await set_state(admin_engine, item, "ready", variants='{"thumb": {}, "medium": {}}')
    old_enough(storage, *[key_of(item) for item in created])

    first = await housekeeping.sweep_orphan_objects(sessionmaker, storage, max_removals=2)
    second = await housekeeping.sweep_orphan_objects(sessionmaker, storage, max_removals=2)

    assert (first.scrubbed, second.scrubbed) == (2, 1)


class RefusesToScrub(InMemoryObjectStorage):
    """Запись пустого объекта вместо оригинала не удаётся, остальное хранилище в порядке."""

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        raise StorageUnavailableError("write is refused")


async def test_a_refused_scrub_does_not_abort_the_rest_of_the_sweep(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    """Дефект из ревью: первый же сбой записи обрывал сверку, и сироты ждали ещё сутки."""
    user = await verified_user(client, jobs)
    photo = await uploaded(client, user, storage, JPEG)
    await set_state(admin_engine, photo, "ready", variants='{"thumb": {}, "medium": {}}')
    refusing = RefusesToScrub()  # то же содержимое, но запись пустого объекта отклоняется
    refusing.page_size = 1  # сирота лежит на странице после отказавшей замены
    refusing.put(key_of(photo), JPEG)
    orphan = "uploads/ffffffff-ffff-4fff-8fff-ffffffffffff/original"  # по ключу идёт после фото
    refusing.put(orphan, b"x")
    old_enough(refusing, key_of(photo), orphan)

    result = await housekeeping.sweep_orphan_objects(sessionmaker, refusing)

    assert result.scrubbed == 0
    assert orphan in refusing.deleted  # сирота убран, несмотря на отказ с оригиналом
    assert refusing.objects[key_of(photo)][0] == JPEG  # оригинал ждёт следующей ночи


async def test_the_sweep_is_capped_per_run_and_continues_next_time(
    sessionmaker: Sessions, storage: InMemoryObjectStorage
) -> None:
    keys = [orphan_key() for _ in range(5)]
    for key in keys:
        storage.put(key, b"x")
    old_enough(storage, *keys)

    first = await housekeeping.sweep_orphan_objects(sessionmaker, storage, max_removals=2)
    second = await housekeeping.sweep_orphan_objects(sessionmaker, storage, max_removals=2)
    third = await housekeeping.sweep_orphan_objects(sessionmaker, storage, max_removals=2)

    assert [(r.orphans_found, r.removed) for r in (first, second, third)] == [
        (5, 2),
        (3, 2),
        (1, 1),
    ]
    assert storage.objects == {}


async def test_the_sweep_reads_the_storage_page_by_page(
    sessionmaker: Sessions, storage: InMemoryObjectStorage
) -> None:
    storage.page_size = 2
    keys = [orphan_key() for _ in range(5)]
    for key in keys:
        storage.put(key, b"x")
    old_enough(storage, *keys)

    result = await housekeeping.sweep_orphan_objects(sessionmaker, storage)

    assert (result.scanned, result.removed) == (5, 5)


async def test_the_sweep_task_works_with_the_storage_of_the_worker_and_fails_loudly_without_it(
    sessionmaker: Sessions,
    storage: InMemoryObjectStorage,
    jobs: InMemoryJobQueue,
    test_settings: Settings,
) -> None:
    key = orphan_key()
    storage.put(key, b"x")
    old_enough(storage, key)
    context = context_for(sessionmaker, storage, jobs, test_settings)

    assert await tasks.sweep_orphan_objects(context) == {
        "scanned": 1,
        "orphans_found": 1,
        "removed": 1,
        "scrubbed": 0,
    }

    storage.unavailable = True
    with pytest.raises(StorageUnavailableError):
        await tasks.sweep_orphan_objects(context)


# ----------------------------------------------------------------------------- соединение БД и хранилище
async def test_deleting_objects_does_not_hold_a_database_connection_while_the_storage_works(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    single_connection_sessions: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted', deleted_at = now() WHERE id = :id",
        id=uuid.UUID(asset_id),
    )
    probing = ProbingStorage(single_connection_sessions)
    probing.put(key_of(created), b"x")

    done = await housekeeping.delete_media_objects(
        single_connection_sessions, probing, [uuid.UUID(asset_id)], link_lifetime=NO_WINDOW
    )

    assert (done, probing.probes) == (1, 1)  # пробный запрос в пуле из одного соединения прошёл
    assert key_of(created) not in probing.objects
    assert (await row_of(admin_engine, asset_id))["objects_deleted_at"] is not None


# ----------------------------------------------------------------------------- воркер
def test_the_media_worker_knows_its_two_tasks_and_the_default_worker_the_two_crons() -> None:
    assert {function.name for function in functions_for(QUEUE_MEDIA)} == {
        "process_media",
        "delete_media_objects",
    }
    names = {job.name for job in cron_jobs_for("default")}
    assert {"cleanup_pending_uploads", "reconcile_uploads"} <= names


async def test_arq_carries_a_file_from_the_api_to_the_media_worker(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    test_settings: Settings,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Токен берём у обычного приложения, а само API здесь с настоящей очередью arq (Redis).
    user = await verified_user(client, jobs)

    def the_same_storage(_settings: Settings) -> InMemoryObjectStorage:
        return storage

    monkeypatch.setattr("messunjerr.jobs.worker.build_storage", the_same_storage)
    application = create_app(test_settings, storage=storage)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as real_queue_client:
            created = await started(real_queue_client, user, size_bytes=len(JPEG))
            put_object(storage, created, JPEG)
            done = await real_queue_client.post(
                f"/api/v1/media/uploads/{created['asset']['id']}/complete", headers=user.headers
            )
            assert done.status_code == 202
            assert (await row_of(admin_engine, created["asset"]["id"]))["status"] == "uploaded"

            # Воркер очереди media разбирает накопленное и выходит.
            worker = build_worker(QUEUE_MEDIA, test_settings, burst=True, handle_signals=False)
            await worker.async_run()
            await worker.close()

            final = await real_queue_client.get(
                f"/api/v1/media/{created['asset']['id']}", headers=user.headers
            )
    assert final.json()["status"] == "ready"
    assert await events_of(admin_engine) == ["AssetUploaded", "AssetProcessed"]

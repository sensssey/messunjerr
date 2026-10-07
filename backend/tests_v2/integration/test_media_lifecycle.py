"""Жизненный цикл ресурса (S5-05): завершение загрузки, чтение, удаление, квота.

Хранилище подставное: тест кладёт объект методом `put`, как это сделал бы клиент по presigned-ссылке.
"""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import QUEUE_MEDIA, InMemoryJobQueue
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.complete_upload import CompleteUpload, complete_upload
from messunjerr.media.infra.memory import InMemoryObjectStorage

from .helpers import bearer, execute, fetch_all, fetch_one, verified_user
from .media_helpers import (
    JPEG,
    MEDIA,
    ProbingStorage,
    complete,
    put_object,
    read_asset,
    started,
    uploaded,
)


async def row_of(engine: AsyncEngine, asset_id: str) -> dict[str, Any]:
    return await fetch_one(
        engine, "SELECT * FROM media.assets WHERE id = :id", id=uuid.UUID(asset_id)
    )


async def events_of(engine: AsyncEngine) -> list[str]:
    rows = await fetch_all(
        engine, "SELECT event_type FROM platform.outbox WHERE topic = 'mj.media.v1' ORDER BY id"
    )
    return [row["event_type"] for row in rows]


# ----------------------------------------------------------------------------- complete
async def test_complete_moves_the_asset_to_uploaded_and_queues_processing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    put_object(storage, created, JPEG)
    asset_id = created["asset"]["id"]

    response = await complete(client, user, asset_id)

    assert response.status_code == 202, response.text
    asset = response.json()["asset"]
    assert asset["status"] == "uploaded"
    assert asset["size_bytes"] == len(JPEG)
    assert asset["uploaded_at"] is not None
    assert asset["urls"] == {"thumb": None, "medium": None, "original": None}

    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["size_bytes"]) == ("uploaded", len(JPEG))
    assert await events_of(admin_engine) == ["AssetUploaded"]
    event = await fetch_one(
        admin_engine, "SELECT key, payload FROM platform.outbox WHERE event_type = 'AssetUploaded'"
    )
    assert event["key"] == asset_id
    assert event["payload"] == {
        "asset_id": asset_id,
        "owner_id": user.user_id,
        "purpose": "post",
        "kind": "image",
    }
    (job,) = jobs.named("process_media")
    assert job.queue == QUEUE_MEDIA
    assert job.job_id == f"process_media:{asset_id}"
    assert job.kwargs == {"asset_id": asset_id}


async def test_a_repeated_complete_returns_the_current_state_and_queues_nothing_more(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]

    again = await complete(client, user, asset_id)

    assert again.status_code == 202
    assert again.json()["asset"]["status"] == "uploaded"
    assert len(jobs.named("process_media")) == 1
    assert await events_of(admin_engine) == ["AssetUploaded"]


async def test_a_repeated_complete_after_processing_shows_the_final_state(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(asset_id),
    )

    again = await complete(client, user, asset_id)

    assert again.status_code == 202
    assert again.json()["asset"]["status"] == "ready"


async def test_complete_without_the_object_is_a_conflict_and_the_asset_stays_pending(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user)
    asset_id = created["asset"]["id"]

    response = await complete(client, user, asset_id)

    assert response.status_code == 409
    assert response.json()["code"] == "upload_missing"
    assert (await row_of(admin_engine, asset_id))["status"] == "pending"
    assert jobs.named("process_media") == []


async def test_the_client_can_finish_after_a_missing_object_error(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    assert (await complete(client, user, created["asset"]["id"])).status_code == 409
    put_object(storage, created, JPEG)  # дозагрузил и повторил
    assert (await complete(client, user, created["asset"]["id"])).status_code == 202


@pytest.mark.parametrize(
    ("declared", "actual", "reason"),
    [
        (1000, 999, "size_mismatch"),
        (1000, 1001, "size_mismatch"),
        (100, 10 * 1024 * 1024 + 1, "size_exceeds_limit"),
    ],
)
async def test_a_wrong_size_rejects_the_asset_and_removes_the_object(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
    declared: int,
    actual: int,
    reason: str,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=declared)
    put_object(storage, created, b"\x00" * actual)
    asset_id = created["asset"]["id"]

    response = await complete(client, user, asset_id)

    assert response.status_code == 422
    problem = response.json()
    assert (problem["code"], problem["reason"]) == ("upload_rejected", reason)
    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["reject_reason"]) == ("rejected", reason)
    assert await events_of(admin_engine) == ["AssetRejected"]
    (job,) = jobs.named("delete_media_objects")
    assert job.kwargs == {"asset_ids": [asset_id]}
    assert jobs.named("process_media") == []

    # Повтор отдаёт то же состояние, а не новую ошибку: клиент мог не получить первый ответ.
    again = await complete(client, user, asset_id)
    assert again.status_code == 202
    assert again.json()["asset"]["status"] == "rejected"
    assert again.json()["asset"]["reject_reason"] == reason


async def test_another_persons_asset_looks_like_it_does_not_exist(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    owner = await verified_user(client, jobs)
    stranger = await verified_user(client, jobs)
    created = await started(client, owner)
    put_object(storage, created, JPEG)

    response = await complete(client, stranger, created["asset"]["id"])

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_unknown_and_malformed_ids(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    user = await verified_user(client, jobs)
    assert (await complete(client, user, uuid.uuid4())).status_code == 404
    malformed = await complete(client, user, "not-a-uuid")
    assert malformed.status_code == 422
    assert malformed.json()["errors"][0]["pointer"] == "/path/asset_id"


async def test_an_unavailable_storage_does_not_change_the_asset(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    put_object(storage, created, JPEG)
    storage.unavailable = True

    response = await complete(client, user, created["asset"]["id"])

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert (await row_of(admin_engine, created["asset"]["id"]))["status"] == "pending"


async def test_a_lost_job_does_not_fail_the_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    # Состояние уже в БД, постановку подберёт reconcile_uploads: клиенту не за что получать 5xx.
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    put_object(storage, created, JPEG)
    jobs.fail_with = RuntimeError("redis is down")

    response = await complete(client, user, created["asset"]["id"])

    assert response.status_code == 202
    assert response.json()["asset"]["status"] == "uploaded"


async def outbox_events(engine: AsyncEngine, event_type: str) -> int:
    row = await fetch_one(
        engine,
        "SELECT count(*) AS n FROM platform.outbox WHERE topic = 'mj.media.v1' AND event_type = :t",
        t=event_type,
    )
    return int(row["n"])


async def test_two_parallel_completions_queue_processing_once(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    put_object(storage, created, JPEG)

    responses = await asyncio.gather(
        *(complete(client, user, created["asset"]["id"]) for _ in range(4))
    )

    assert [response.status_code for response in responses] == [202] * 4
    assert len(jobs.named("process_media")) == 1
    # Очередь в памяти сама отбрасывает повторный job_id, поэтому важнее другое: строка перешла в
    # `uploaded` один раз, и событие в outbox одно.
    assert await outbox_events(admin_engine, "AssetUploaded") == 1


async def test_completing_and_deleting_at_the_same_time_leave_a_consistent_state(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    asset_id = created["asset"]["id"]
    put_object(storage, created, JPEG)

    done, removed = await asyncio.gather(
        complete(client, user, asset_id),
        client.delete(f"{MEDIA}/{asset_id}", headers=user.headers),
    )

    # Любой порядок допустим, но итог один: ресурс удалён, а `complete` либо успел, либо видит 404.
    assert removed.status_code == 204
    assert done.status_code in (202, 404)
    assert (await row_of(admin_engine, asset_id))["status"] == "deleted"
    assert await outbox_events(admin_engine, "AssetDeleted") == 1
    assert await outbox_events(admin_engine, "AssetUploaded") == (
        1 if done.status_code == 202 else 0
    )


# ----------------------------------------------------------------------------- чтение
async def test_the_owner_reads_the_asset(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)

    response = await read_asset(client, user, created["asset"]["id"])

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    asset = response.json()
    assert asset["id"] == created["asset"]["id"]
    assert asset["status"] == "uploaded"
    assert set(asset) == {
        "id", "purpose", "kind", "status", "filename", "content_type", "declared_size",
        "size_bytes", "width", "height", "reject_reason", "urls", "url_expires_at",
        "created_at", "uploaded_at", "processed_at",
    }  # fmt: skip


async def test_a_stranger_and_an_anonymous_client_cannot_read_it(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(client, jobs)
    stranger = await verified_user(client, jobs)
    created = await started(client, owner)
    asset_id = created["asset"]["id"]

    assert (await read_asset(client, stranger, asset_id)).status_code == 404
    assert (await client.get(f"{MEDIA}/{asset_id}")).status_code == 401
    assert (await read_asset(client, owner, uuid.uuid4())).status_code == 404


# ----------------------------------------------------------------------------- удаление
async def test_delete_marks_the_asset_deleted_and_queues_the_object_removal(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]

    response = await client.delete(f"{MEDIA}/{asset_id}", headers=user.headers)

    assert response.status_code == 204
    assert response.content == b""
    row = await row_of(admin_engine, asset_id)
    assert row["status"] == "deleted"
    assert row["deleted_at"] is not None
    assert row["objects_deleted_at"] is None  # объекты уберёт задача
    assert await events_of(admin_engine) == ["AssetUploaded", "AssetDeleted"]
    (job,) = jobs.named("delete_media_objects")
    assert (job.queue, job.job_id) == (QUEUE_MEDIA, f"delete_media_objects:{asset_id}")
    assert job.kwargs == {"asset_ids": [asset_id]}


async def test_a_deleted_asset_is_gone_for_every_endpoint(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage)
    asset_id = created["asset"]["id"]
    await client.delete(f"{MEDIA}/{asset_id}", headers=user.headers)

    assert (await read_asset(client, user, asset_id)).status_code == 404
    assert (await complete(client, user, asset_id)).status_code == 404
    assert (await client.delete(f"{MEDIA}/{asset_id}", headers=user.headers)).status_code == 404


async def test_a_pending_asset_can_be_deleted_before_the_upload_is_finished(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user)
    assert (
        await client.delete(f"{MEDIA}/{created['asset']['id']}", headers=user.headers)
    ).status_code == 204


async def test_someone_elses_asset_cannot_be_deleted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner = await verified_user(client, jobs)
    stranger = await verified_user(client, jobs)
    created = await started(client, owner)

    response = await client.delete(f"{MEDIA}/{created['asset']['id']}", headers=stranger.headers)

    assert response.status_code == 404
    assert (await row_of(admin_engine, created["asset"]["id"]))["status"] == "pending"


async def test_an_asset_used_as_an_avatar_cannot_be_deleted(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, purpose="avatar")
    asset_id = uuid.UUID(created["asset"]["id"])
    await execute(
        admin_engine,
        "UPDATE profile.profiles SET avatar_asset_id = :asset WHERE user_id = :user",
        asset=asset_id,
        user=uuid.UUID(user.user_id),
    )

    response = await client.delete(f"{MEDIA}/{asset_id}", headers=user.headers)

    assert response.status_code == 409
    assert response.json()["code"] == "asset_in_use"
    assert (await row_of(admin_engine, str(asset_id)))["status"] == "uploaded"
    assert jobs.named("delete_media_objects") == []


async def test_deleting_the_asset_row_clears_the_avatar_reference(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    # Внешний ключ профиля на ресурс: ON DELETE SET NULL (4.5).
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, purpose="avatar")
    asset_id = uuid.UUID(created["asset"]["id"])
    await execute(
        admin_engine,
        "UPDATE profile.profiles SET avatar_asset_id = :asset WHERE user_id = :user",
        asset=asset_id,
        user=uuid.UUID(user.user_id),
    )

    await execute(admin_engine, "DELETE FROM media.assets WHERE id = :id", id=asset_id)

    profile = await fetch_one(
        admin_engine,
        "SELECT avatar_asset_id FROM profile.profiles WHERE user_id = :u",
        u=uuid.UUID(user.user_id),
    )
    assert profile["avatar_asset_id"] is None


async def test_deleting_the_account_removes_its_assets_rows(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    await uploaded(client, user, storage)
    await execute(
        admin_engine, "DELETE FROM identity.users WHERE id = :id", id=uuid.UUID(user.user_id)
    )
    assert await fetch_all(admin_engine, "SELECT 1 FROM media.assets") == []


# ----------------------------------------------------------------------------- квота
async def test_the_quota_endpoint_counts_ready_files_and_reserved_uploads(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
    test_settings: Any,
) -> None:
    user = await verified_user(client, jobs)
    empty = await client.get(f"{MEDIA}/quota", headers=user.headers)
    assert empty.json() == {
        "used_bytes": 0,
        "limit_bytes": test_settings.media_quota_bytes,
        "assets_count": 0,
    }

    ready = await uploaded(client, user, storage, size_bytes=len(JPEG))
    await started(client, user, size_bytes=4000)  # ещё грузится: резерв заявленного размера
    rejected = await started(client, user, size_bytes=700)
    put_object(storage, rejected, b"\x00" * 5)
    await complete(client, user, rejected["asset"]["id"])  # отклонён: место не занимает
    deleted = await started(client, user, size_bytes=9000)
    await client.delete(f"{MEDIA}/{deleted['asset']['id']}", headers=user.headers)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready', size_bytes = 1500 WHERE id = :id",
        id=uuid.UUID(ready["asset"]["id"]),
    )

    quota = (await client.get(f"{MEDIA}/quota", headers=user.headers)).json()

    assert quota["used_bytes"] == 1500 + 4000  # готовый по факту + загружаемый по заявке
    assert quota["assets_count"] == 2


async def test_quota_is_per_person_and_needs_a_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    anna = await verified_user(client, jobs)
    boris = await verified_user(client, jobs)
    await started(client, anna, size_bytes=1234)
    assert (await client.get(f"{MEDIA}/quota", headers=anna.headers)).json()["used_bytes"] == 1234
    assert (await client.get(f"{MEDIA}/quota", headers=boris.headers)).json()["used_bytes"] == 0
    assert (await client.get(f"{MEDIA}/quota")).status_code == 401
    # «quota» не должно разбираться как идентификатор ресурса.
    assert (
        await client.get(f"{MEDIA}/quota", headers=bearer(boris.auth["access_token"]))
    ).status_code == 200


async def test_completing_does_not_hold_a_database_connection_while_the_storage_answers(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    single_connection_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Зависшее хранилище не должно вытеснять из пула остальные запросы (пул тут из одного соединения)."""
    user = await verified_user(client, jobs)
    created = await started(client, user, size_bytes=len(JPEG))
    probing = ProbingStorage(single_connection_sessions)
    put_object(probing, created, JPEG)

    async with UnitOfWork(single_connection_sessions) as uow:
        asset = await complete_upload(
            CompleteUpload(
                owner_id=uuid.UUID(user.user_id), asset_id=uuid.UUID(created["asset"]["id"])
            ),
            uow=uow,
            storage=probing,
            jobs=jobs,
        )

    assert (asset.status, probing.probes) == ("uploaded", 1)

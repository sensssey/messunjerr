"""Аватары (S6-04): назначение готового ресурса, ошибки проверки, замена и очистка прежнего.

Профили стоят ниже медиа и знают о нём только через порт `AvatarAssets`; тест идёт через HTTP, как
клиент, и смотрит, что остаётся в БД и в хранилище.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.media.commands import housekeeping
from messunjerr.media.infra.memory import InMemoryObjectStorage

from .helpers import SignedInUser, execute, fetch_all, fetch_one, verified_user, view
from .media_helpers import GIF, JPEG, MEDIA, PNG, ready, uploaded

PROFILE = "/api/v1/me/profile"
Sessions = async_sessionmaker[AsyncSession]


def errors_of(response: httpx.Response) -> list[tuple[str, str]]:
    return [(item["pointer"], item["code"]) for item in response.json()["errors"]]


async def set_avatar(
    client: httpx.AsyncClient, user: SignedInUser, asset_id: str | None
) -> httpx.Response:
    return await client.patch(PROFILE, json={"avatar_asset_id": asset_id}, headers=user.headers)


async def status_of(engine: AsyncEngine, asset_id: str) -> dict[str, Any]:
    return await fetch_one(
        engine,
        "SELECT status, deleted_at, objects_deleted_at FROM media.assets WHERE id = :id",
        id=uuid.UUID(asset_id),
    )


def public_urls(asset_id: str) -> dict[str, str]:
    base = f"/media/public/avatars/{asset_id}"
    return {"sm": f"{base}/64.webp", "md": f"{base}/256.webp"}


# ----------------------------------------------------------------------------- назначение
async def test_a_ready_avatar_is_set_and_visible_to_the_owner_and_to_others(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    anna = await verified_user(client, jobs)
    boris = await verified_user(client, jobs)
    asset_id = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")

    response = await set_avatar(client, anna, asset_id)

    assert response.status_code == 200, response.text
    assert response.json()["avatar"] == public_urls(asset_id)
    me = await client.get("/api/v1/me", headers=anna.headers)
    assert me.json()["profile"]["avatar"] == public_urls(asset_id)
    seen = await view(client, boris, anna.credentials["username"])
    assert seen.json()["user"]["avatar"] == public_urls(asset_id)


async def test_a_profile_without_an_avatar_has_none(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    anna = await verified_user(client, jobs)

    assert (await client.get("/api/v1/me", headers=anna.headers)).json()["profile"][
        "avatar"
    ] is None


@pytest.mark.parametrize("state", ["pending", "uploaded", "processing", "rejected"])
async def test_an_asset_that_is_not_ready_cannot_be_an_avatar(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
    state: str,
) -> None:
    anna = await verified_user(client, jobs)
    created = await uploaded(
        client, anna, storage, JPEG, purpose="avatar", content_type="image/jpeg"
    )
    asset_id = created["asset"]["id"]
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = :state WHERE id = :id",
        state=state,
        id=uuid.UUID(asset_id),
    )

    response = await set_avatar(client, anna, asset_id)

    assert response.status_code == 422
    assert errors_of(response) == [("/body/avatar_asset_id", "asset_not_ready")]


async def test_a_ready_image_of_the_s5_era_without_variants_is_not_ready_yet(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    created = await uploaded(
        client, anna, storage, JPEG, purpose="avatar", content_type="image/jpeg"
    )
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )

    response = await set_avatar(client, anna, created["asset"]["id"])

    assert errors_of(response) == [("/body/avatar_asset_id", "asset_not_ready")]


@pytest.mark.parametrize(
    ("purpose", "body", "content_type"),
    [
        ("post", JPEG, "image/jpeg"),
        ("group_avatar", PNG, "image/png"),
        ("message", PNG, "image/png"),
    ],
)
async def test_an_asset_with_another_purpose_cannot_be_an_avatar(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    purpose: str,
    body: bytes,
    content_type: str,
) -> None:
    anna = await verified_user(client, jobs)
    asset_id = await ready(
        client, anna, storage, jobs, sessionmaker, body, purpose=purpose, content_type=content_type
    )

    response = await set_avatar(client, anna, asset_id)

    assert errors_of(response) == [("/body/avatar_asset_id", "asset_wrong_purpose")]


async def test_someone_elses_missing_and_deleted_assets_are_all_not_found(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    boris = await verified_user(client, jobs)
    theirs = await ready(client, boris, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    deleted = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted', deleted_at = now() WHERE id = :id",
        id=uuid.UUID(deleted),
    )

    for asset_id in (theirs, str(uuid.uuid4()), deleted):
        response = await set_avatar(client, anna, asset_id)
        assert errors_of(response) == [("/body/avatar_asset_id", "asset_not_found")]
    assert (await client.get("/api/v1/me", headers=anna.headers)).json()["profile"][
        "avatar"
    ] is None


# ----------------------------------------------------------------------------- замена и очистка
async def test_replacing_the_avatar_deletes_the_previous_one_and_frees_its_quota(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    first = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    second = await ready(client, anna, storage, jobs, sessionmaker, PNG, purpose="avatar")
    assert (await set_avatar(client, anna, first)).status_code == 200
    jobs.clear()

    response = await set_avatar(client, anna, second)

    assert response.status_code == 200
    assert response.json()["avatar"] == public_urls(second)
    assert (await status_of(admin_engine, first))["status"] == "deleted"
    assert (await status_of(admin_engine, second))["status"] == "ready"
    assert [job.kwargs for job in jobs.named("delete_media_objects")] == [{"asset_ids": [first]}]
    events = await fetch_all(
        admin_engine,
        "SELECT event_type, payload->>'asset_id' AS asset FROM platform.outbox "
        "WHERE topic = 'mj.media.v1' AND event_type = 'AssetDeleted'",
    )
    assert [row["asset"] for row in events] == [first]
    assert (await client.get(f"{MEDIA}/{first}", headers=anna.headers)).status_code == 404

    # Объекты прежнего аватара убирает задача; адрес нового остаётся.
    assert f"public/avatars/{first}/256.webp" in storage.objects
    await housekeeping.delete_media_objects(
        sessionmaker, storage, [uuid.UUID(first)], link_lifetime=timedelta(0)
    )
    assert not [key for key in storage.objects if first in key]
    assert f"public/avatars/{second}/256.webp" in storage.objects
    quota = (await client.get(f"{MEDIA}/quota", headers=anna.headers)).json()
    assert quota["assets_count"] == 1


async def test_clearing_the_avatar_deletes_the_asset_too(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    asset_id = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    await set_avatar(client, anna, asset_id)
    jobs.clear()

    response = await set_avatar(client, anna, None)

    assert response.status_code == 200
    assert response.json()["avatar"] is None
    assert (await status_of(admin_engine, asset_id))["status"] == "deleted"
    assert len(jobs.named("delete_media_objects")) == 1
    assert (await client.get("/api/v1/me", headers=anna.headers)).json()["profile"][
        "avatar"
    ] is None


async def test_setting_the_same_avatar_again_or_editing_other_fields_keeps_it(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    asset_id = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    await set_avatar(client, anna, asset_id)
    jobs.clear()

    again = await set_avatar(client, anna, asset_id)
    renamed = await client.patch(PROFILE, json={"display_name": "Анна"}, headers=anna.headers)
    cleared_bio = await client.patch(PROFILE, json={"bio": None}, headers=anna.headers)

    assert [r.status_code for r in (again, renamed, cleared_bio)] == [200, 200, 200]
    assert renamed.json()["avatar"] == public_urls(asset_id)
    assert (await status_of(admin_engine, asset_id))["status"] == "ready"
    assert jobs.named("delete_media_objects") == []


async def test_a_failed_validation_does_not_touch_the_current_avatar(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    asset_id = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    await set_avatar(client, anna, asset_id)
    jobs.clear()

    response = await set_avatar(client, anna, str(uuid.uuid4()))

    assert response.status_code == 422
    assert (await status_of(admin_engine, asset_id))["status"] == "ready"
    assert jobs.named("delete_media_objects") == []
    assert (await client.get("/api/v1/me", headers=anna.headers)).json()["profile"][
        "avatar"
    ] == public_urls(asset_id)


async def test_an_attached_avatar_cannot_be_deleted_directly_but_a_replaced_one_is_gone(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    anna = await verified_user(client, jobs)
    first = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")
    second = await ready(client, anna, storage, jobs, sessionmaker, PNG, purpose="avatar")
    await set_avatar(client, anna, first)

    attached = await client.delete(f"{MEDIA}/{first}", headers=anna.headers)
    await set_avatar(client, anna, second)
    replaced = await client.delete(f"{MEDIA}/{first}", headers=anna.headers)

    assert (attached.status_code, attached.json()["code"]) == (409, "asset_in_use")
    assert replaced.status_code == 404  # уже удалён заменой


async def test_a_gif_cannot_become_an_avatar_even_as_a_post_attachment(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    anna = await verified_user(client, jobs)
    asset_id = await ready(
        client, anna, storage, jobs, sessionmaker, GIF, purpose="post", content_type="image/gif"
    )

    assert errors_of(await set_avatar(client, anna, asset_id)) == [
        ("/body/avatar_asset_id", "asset_wrong_purpose")
    ]


# ----------------------------------------------------------------------------- гонки
async def test_deleting_and_assigning_at_the_same_time_never_leaves_a_dangling_avatar(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    anna = await verified_user(client, jobs)
    for _ in range(6):
        asset_id = await ready(client, anna, storage, jobs, sessionmaker, JPEG, purpose="avatar")

        assign, delete = await asyncio.gather(
            set_avatar(client, anna, asset_id),
            client.delete(f"{MEDIA}/{asset_id}", headers=anna.headers),
        )

        profile = (await client.get("/api/v1/me", headers=anna.headers)).json()["profile"]
        status = (await status_of(admin_engine, asset_id))["status"]
        if assign.status_code == 200:  # назначение успело первым: удалять привязанное нельзя
            assert (delete.status_code, profile["avatar"]) == (409, public_urls(asset_id))
            assert status == "ready"
            await set_avatar(client, anna, None)  # освободить для следующего круга
        else:  # удаление успело первым: назначать нечего
            assert (assign.status_code, delete.status_code) == (422, 204)
            assert profile["avatar"] is None
            assert status == "deleted"

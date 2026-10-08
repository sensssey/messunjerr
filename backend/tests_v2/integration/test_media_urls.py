"""Ссылки на файлы (S6-03): `GET /media/{asset_id}/urls`, кто их получает и какими они бывают."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.settings import Settings

from .helpers import client_with, execute, verified_user
from .media_helpers import (
    GIF,
    JPEG,
    MEDIA,
    PDF,
    read_urls,
    ready,
    started,
    uploaded,
)

Sessions = async_sessionmaker[AsyncSession]
EMPTY = {"thumb": None, "medium": None, "original": None}


def expires_in(body: dict[str, object]) -> float:
    stamp = datetime.fromisoformat(str(body["url_expires_at"]).replace("Z", "+00:00"))
    return (stamp - datetime.now(UTC)).total_seconds()


async def test_the_owner_gets_fresh_links_for_a_ready_photo(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    asset_id = await ready(client, user, storage, jobs, sessionmaker, JPEG)

    response = await read_urls(client, user, asset_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"urls", "url_expires_at"}
    assert f"{asset_id}/thumb.webp" in body["urls"]["thumb"]
    assert f"{asset_id}/medium.webp" in body["urls"]["medium"]
    assert body["urls"]["original"] is None
    assert 590 < expires_in(body) <= 600  # ссылки живут 10 минут
    assert response.headers["cache-control"] == "no-store"  # ссылка с подписью не для кэша


async def test_every_call_signs_new_links(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    asset_id = await ready(client, user, storage, jobs, sessionmaker, JPEG)
    before = len(storage.presigned_gets)

    await read_urls(client, user, asset_id)
    await read_urls(client, user, asset_id)

    assert len(storage.presigned_gets) == before + 4  # по два варианта за вызов


async def test_a_gif_also_gets_a_link_to_its_original(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    asset_id = await ready(client, user, storage, jobs, sessionmaker, GIF, content_type="image/gif")

    urls = (await read_urls(client, user, asset_id)).json()["urls"]

    assert urls["original"] is not None
    assert urls["thumb"] is not None
    assert urls["medium"] is not None


async def test_a_file_has_only_the_original_with_a_forced_download(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    asset_id = await ready(
        client,
        user,
        storage,
        jobs,
        sessionmaker,
        PDF,
        purpose="message",
        filename="report.pdf",
        content_type="application/pdf",
    )

    body = (await read_urls(client, user, asset_id)).json()

    assert body["urls"]["thumb"] is None
    assert body["urls"]["medium"] is None
    assert f"{asset_id}/original" in body["urls"]["original"]
    assert "response-content-disposition=attachment" in body["urls"]["original"]
    assert "filename" in body["urls"]["original"]
    assert 590 < expires_in(body) <= 600


async def test_an_avatar_has_public_links_that_never_expire(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    asset_id = await ready(client, user, storage, jobs, sessionmaker, JPEG, purpose="avatar")

    body = (await read_urls(client, user, asset_id)).json()

    assert body["urls"]["thumb"].endswith(f"/public/avatars/{asset_id}/64.webp")
    assert body["urls"]["medium"].endswith(f"/public/avatars/{asset_id}/256.webp")
    assert "X-Amz" not in body["urls"]["thumb"]  # подписи нет: адрес открыт всем
    assert body["url_expires_at"] is None
    assert storage.presigned_gets == []


@pytest.mark.parametrize("state", ["pending", "uploaded", "processing", "rejected"])
async def test_an_asset_that_is_not_ready_has_no_links(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
    state: str,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = :state WHERE id = :id",
        state=state,
        id=uuid.UUID(created["asset"]["id"]),
    )

    response = await read_urls(client, user, created["asset"]["id"])

    assert response.status_code == 200
    assert response.json() == {"urls": EMPTY, "url_expires_at": None}


async def test_a_ready_image_of_the_s5_era_without_variants_has_no_links_yet(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )

    assert (await read_urls(client, user, created["asset"]["id"])).json()["urls"] == EMPTY


async def test_a_stranger_cannot_get_links_and_cannot_tell_why(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    owner = await verified_user(client, jobs)
    stranger = await verified_user(client, jobs)
    asset_id = await ready(client, owner, storage, jobs, sessionmaker, JPEG)
    removed = await ready(client, owner, storage, jobs, sessionmaker, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'deleted' WHERE id = :id",
        id=uuid.UUID(removed),
    )

    responses = [
        await read_urls(client, stranger, asset_id),  # чужой
        await read_urls(client, owner, str(uuid.uuid4())),  # несуществующий
        await read_urls(client, owner, removed),  # удалённый
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert all(response.json()["code"] == "not_found" for response in responses)


class GrantedTo:
    """Привязка, которой пока нет в продукте: пост или беседа, видимые определённым людям."""

    def __init__(self) -> None:
        self.allowed: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.asked: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def can_view(
        self, session: AsyncSession, *, asset_id: uuid.UUID, viewer_id: uuid.UUID
    ) -> bool:
        self.asked.append((asset_id, viewer_id))
        return (asset_id, viewer_id) in self.allowed


async def test_someone_who_may_see_the_object_the_asset_is_attached_to_gets_links(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
) -> None:
    owner = await verified_user(client, jobs)
    friend = await verified_user(client, jobs)
    outsider = await verified_user(client, jobs)
    asset_id = await ready(client, owner, storage, jobs, sessionmaker, JPEG)
    audience = GrantedTo()
    audience.allowed.add((uuid.UUID(asset_id), uuid.UUID(friend.user_id)))

    async with client_with(test_settings, jobs, storage=storage, asset_audience=audience) as api:
        seen = await read_urls(api, friend, asset_id)
        hidden = await read_urls(api, outsider, asset_id)
        mine = await read_urls(api, owner, asset_id)

    assert seen.status_code == 200
    assert seen.json()["urls"]["thumb"] is not None
    assert hidden.status_code == 404
    assert mine.status_code == 200
    # Владельца привязки не спрашивают, а остальных спрашивают о конкретном ресурсе и человеке.
    assert audience.asked == [
        (uuid.UUID(asset_id), uuid.UUID(friend.user_id)),
        (uuid.UUID(asset_id), uuid.UUID(outsider.user_id)),
    ]


async def test_the_card_of_a_stranger_is_still_closed_even_when_links_are_shared(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
) -> None:
    owner = await verified_user(client, jobs)
    friend = await verified_user(client, jobs)
    asset_id = await ready(client, owner, storage, jobs, sessionmaker, JPEG)
    audience = GrantedTo()
    audience.allowed.add((uuid.UUID(asset_id), uuid.UUID(friend.user_id)))

    async with client_with(test_settings, jobs, storage=storage, asset_audience=audience) as api:
        card = await api.get(f"{MEDIA}/{asset_id}", headers=friend.headers)

    assert card.status_code == 404  # карточка (имя файла, размеры, квота) только у владельца


async def test_links_need_a_token_and_a_valid_identifier(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    assert (await client.get(f"{MEDIA}/{uuid.uuid4()}/urls")).status_code == 401
    assert (await client.get(f"{MEDIA}/not-a-uuid/urls", headers=user.headers)).status_code == 422


async def test_a_pending_upload_answers_with_empty_links_and_not_an_error(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user)

    response = await read_urls(client, user, created["asset"]["id"])

    assert (response.status_code, response.json()["urls"]) == (200, EMPTY)

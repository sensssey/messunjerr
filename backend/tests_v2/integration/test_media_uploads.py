"""Начало загрузки (S5-04): `POST /media/uploads`, проверки заявки, квота, идемпотентность, лимит.

Хранилище подставное (в памяти): сетевую часть проверяют `test_media_s3.py` и тесты стенда.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.settings import Settings

from .helpers import (
    bearer,
    client_with,
    fetch_all,
    fetch_one,
    limited_client,
    verified_user,
)
from .media_helpers import MIB, UPLOADS, body_of, start, started


def items_of(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 422, response.text
    errors: list[dict[str, Any]] = response.json()["errors"]
    return errors


# ----------------------------------------------------------------------------- успешная заявка
async def test_init_returns_a_pending_asset_and_a_presigned_put(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    before = datetime.now(UTC)

    response = await start(client, user)

    assert response.status_code == 201, response.text
    created = response.json()
    asset, upload = created["asset"], created["upload"]
    assert response.headers["location"] == f"/api/v1/media/{asset['id']}"
    assert response.headers["cache-control"] == "no-store"
    assert asset["status"] == "pending"
    assert (asset["purpose"], asset["kind"]) == ("post", "image")
    assert (asset["filename"], asset["content_type"]) == ("photo.jpg", "image/jpeg")
    assert asset["declared_size"] == 1000
    assert asset["size_bytes"] is None
    assert asset["urls"] == {"thumb": None, "medium": None, "original": None}
    assert asset["uploaded_at"] is None
    assert upload["method"] == "PUT"
    # Запись один раз: повторный PUT по ссылке получает 412 (подпись требует этот заголовок).
    assert upload["headers"] == {"Content-Type": "image/jpeg", "If-None-Match": "*"}
    assert upload["url"].startswith(f"{storage.base_url}/media/uploads/{asset['id']}/original")
    expires = datetime.fromisoformat(upload["expires_at"])
    assert timedelta(minutes=14, seconds=50) < expires - before < timedelta(minutes=15, seconds=10)

    (signed,) = storage.presigned
    assert signed.key == f"uploads/{asset['id']}/original"
    assert (signed.content_type, signed.content_length, signed.expires_in) == (
        "image/jpeg",
        1000,
        900,
    )

    row = await fetch_one(
        admin_engine, "SELECT * FROM media.assets WHERE id = :id", id=uuid.UUID(asset["id"])
    )
    assert str(row["owner_id"]) == user.user_id
    assert (row["status"], row["kind"], row["purpose"]) == ("pending", "image", "post")
    assert row["object_key"] == signed.key
    assert (row["declared_size"], row["size_bytes"]) == (1000, None)
    assert row["variants"] == {}
    assert (
        await fetch_all(admin_engine, "SELECT 1 FROM platform.outbox WHERE topic = 'mj.media.v1'")
        == []
    )


async def test_files_are_signed_as_octet_stream_but_keep_their_declared_type(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    user = await verified_user(client, jobs)
    created = await started(
        client,
        user,
        purpose="message",
        filename="report.pdf",
        content_type="application/PDF; charset=binary",
        size_bytes=5000,
    )
    assert created["asset"]["kind"] == "file"
    assert created["asset"]["content_type"] == "application/pdf"
    assert created["upload"]["headers"] == {
        "Content-Type": "application/octet-stream",
        "If-None-Match": "*",
    }
    assert storage.presigned[0].content_type == "application/octet-stream"


async def test_svg_is_a_file_never_an_image(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, filename="logo.svg", content_type="image/svg+xml")
    assert created["asset"]["kind"] == "file"
    assert created["upload"]["headers"]["Content-Type"] == "application/octet-stream"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("C:\\fakepath\\tab\there.png", "tab here.png"),
        ("../../etc/passwd", "passwd"),
        ("evil\u202egpj.png", "evilgpj.png"),
        ("   ", "file"),
    ],
)
async def test_filenames_are_cleaned_not_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, raw: str, cleaned: str
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, filename=raw)
    assert created["asset"]["filename"] == cleaned


@pytest.mark.parametrize(
    ("overrides", "limit"),
    [
        ({"purpose": "avatar", "size_bytes": 5 * MIB}, 5 * MIB),
        ({"purpose": "group_avatar", "size_bytes": 5 * MIB}, 5 * MIB),
        ({"purpose": "post", "size_bytes": 10 * MIB}, 10 * MIB),
        ({"purpose": "message", "size_bytes": 10 * MIB}, 10 * MIB),
        (
            {
                "purpose": "post",
                "content_type": "application/zip",
                "filename": "a.zip",
                "size_bytes": 25 * MIB,
            },
            25 * MIB,
        ),
    ],
)
async def test_a_file_exactly_at_the_limit_is_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, overrides: dict[str, Any], limit: int
) -> None:
    user = await verified_user(client, jobs)
    created = await started(client, user, **overrides)
    assert created["asset"]["declared_size"] == limit


# ----------------------------------------------------------------------------- ошибки заявки
@pytest.mark.parametrize("purpose", ["wallpaper", ""])
async def test_unknown_purpose_is_refused_with_the_catalog_code(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, purpose: str
) -> None:
    user = await verified_user(client, jobs)
    items = items_of(await start(client, user, purpose=purpose))
    assert [(item["pointer"], item["code"]) for item in items] == [
        ("/body/purpose", "purpose_invalid")
    ]
    assert "post" in items[0]["meta"]["allowed"]


@pytest.mark.parametrize(
    ("overrides", "pointer", "code"),
    [
        (
            {"purpose": "avatar", "content_type": "image/gif"},
            "/body/content_type",
            "content_type_not_allowed",
        ),
        (
            {"purpose": "group_avatar", "content_type": "application/pdf"},
            "/body/content_type",
            "content_type_not_allowed",
        ),
        ({"content_type": "not a media type"}, "/body/content_type", "content_type_not_allowed"),
        ({"content_type": "image/"}, "/body/content_type", "content_type_not_allowed"),
        ({"content_type": ""}, "/body/content_type", "content_type_not_allowed"),
        ({"filename": "setup.EXE"}, "/body/filename", "extension_forbidden"),
        ({"filename": "run.ps1."}, "/body/filename", "extension_forbidden"),
        ({"size_bytes": 0}, "/body/size_bytes", "size_invalid"),
        ({"size_bytes": -5}, "/body/size_bytes", "size_invalid"),
    ],
)
async def test_invalid_requests_get_one_item_with_the_catalog_code(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    overrides: dict[str, Any],
    pointer: str,
    code: str,
) -> None:
    user = await verified_user(client, jobs)
    items = items_of(await start(client, user, **overrides))
    assert [(item["pointer"], item["code"]) for item in items] == [(pointer, code)]


@pytest.mark.parametrize(
    ("overrides", "max_bytes"),
    [
        ({"purpose": "avatar", "size_bytes": 5 * MIB + 1}, 5 * MIB),
        ({"purpose": "post", "size_bytes": 10 * MIB + 1}, 10 * MIB),
        (
            {
                "purpose": "message",
                "content_type": "application/zip",
                "filename": "a.zip",
                "size_bytes": 25 * MIB + 1,
            },
            25 * MIB,
        ),
        ({"size_bytes": 2**62}, 10 * MIB),
    ],
)
async def test_oversized_files_report_the_limit_of_their_purpose(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, overrides: dict[str, Any], max_bytes: int
) -> None:
    user = await verified_user(client, jobs)
    items = items_of(await start(client, user, **overrides))
    assert [(item["code"], item["meta"]["max_bytes"]) for item in items] == [
        ("size_exceeds_limit", max_bytes)
    ]


async def test_every_problem_of_the_request_is_reported_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    items = items_of(
        await start(
            client, user, purpose="avatar", content_type="image/gif", filename="x.exe", size_bytes=0
        )
    )
    assert {item["code"] for item in items} == {
        "content_type_not_allowed",
        "extension_forbidden",
        "size_invalid",
    }


@pytest.mark.parametrize("size", ["1000", 1000.5, True, None, [1]])
async def test_the_size_must_be_an_integer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, size: Any
) -> None:
    user = await verified_user(client, jobs)
    items = items_of(await start(client, user, size_bytes=size))
    assert items[0]["pointer"] == "/body/size_bytes"


async def test_unknown_and_missing_fields_use_the_general_codes(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    unknown = items_of(await start(client, user, kind="image"))
    assert [(item["pointer"], item["code"]) for item in unknown] == [
        ("/body/kind", "unknown_field")
    ]
    missing = await client.post(UPLOADS, json={"purpose": "post"}, headers=user.headers)
    assert {item["pointer"] for item in items_of(missing)} == {
        "/body/filename",
        "/body/content_type",
        "/body/size_bytes",
    }


async def test_a_refused_request_leaves_no_trace(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    await start(client, user, size_bytes=0)
    assert storage.presigned == []
    assert await fetch_all(admin_engine, "SELECT 1 FROM media.assets") == []


async def test_a_token_is_required(client: httpx.AsyncClient) -> None:
    response = await client.post(UPLOADS, json=body_of())
    assert response.status_code == 401
    assert response.json()["code"] == "token_missing"


# ----------------------------------------------------------------------------- квота
async def test_the_declared_size_of_started_uploads_counts_against_the_quota(
    test_settings: Settings, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    async with client_with(test_settings, jobs, storage=storage, media_quota_bytes=2500) as client:
        user = await verified_user(client, jobs)
        first = await started(client, user)
        await started(client, user)

        refused = await start(client, user)  # третья по 1000 уже не помещается
        assert refused.status_code == 403
        problem = refused.json()
        assert problem["code"] == "quota_exceeded"
        assert (problem["limit"], problem["used"]) == (2500, 2000)

        # Ровно впритык помещается; освобождённое удалением место снова доступно.
        assert (await start(client, user, size_bytes=500)).status_code == 201
        deleted = await client.delete(f"/api/v1/media/{first['asset']['id']}", headers=user.headers)
        assert deleted.status_code == 204
        assert (await start(client, user)).status_code == 201


async def test_the_quota_of_one_person_does_not_touch_another(
    test_settings: Settings, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    async with client_with(test_settings, jobs, storage=storage, media_quota_bytes=1500) as client:
        anna = await verified_user(client, jobs)
        boris = await verified_user(client, jobs)
        await started(client, anna)
        assert (await start(client, anna)).status_code == 403
        assert (await start(client, boris)).status_code == 201


async def test_parallel_requests_cannot_squeeze_through_the_same_free_space(
    test_settings: Settings, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    async with client_with(test_settings, jobs, storage=storage, media_quota_bytes=1500) as client:
        user = await verified_user(client, jobs)
        responses = await asyncio.gather(*(start(client, user) for _ in range(5)))
    assert sorted(response.status_code for response in responses) == [201, 403, 403, 403, 403]


# ----------------------------------------------------------------------------- Idempotency-Key
async def test_the_same_key_and_body_return_the_first_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    headers = {**user.headers, "Idempotency-Key": str(uuid.uuid4())}
    first = await client.post(UPLOADS, json=body_of(), headers=headers)
    again = await client.post(UPLOADS, json=body_of(), headers=headers)

    assert first.status_code == again.status_code == 201
    assert again.headers["idempotency-replayed"] == "true"
    assert "idempotency-replayed" not in first.headers
    assert again.json() == first.json()
    assert again.headers["location"] == first.headers["location"]
    rows = await fetch_all(admin_engine, "SELECT id FROM media.assets")
    assert len(rows) == 1


async def test_the_same_key_with_another_body_is_refused(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    headers = {**user.headers, "Idempotency-Key": str(uuid.uuid4())}
    await client.post(UPLOADS, json=body_of(), headers=headers)
    other = await client.post(UPLOADS, json=body_of(size_bytes=2000), headers=headers)
    assert other.status_code == 422
    assert other.json()["code"] == "idempotency_key_reuse"


async def test_without_a_key_every_request_creates_its_own_asset(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    first = await started(client, user)
    second = await started(client, user)
    assert first["asset"]["id"] != second["asset"]["id"]


async def test_the_key_is_per_person(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    anna = await verified_user(client, jobs)
    boris = await verified_user(client, jobs)
    key = str(uuid.uuid4())
    first = await client.post(
        UPLOADS, json=body_of(), headers={**anna.headers, "Idempotency-Key": key}
    )
    second = await client.post(
        UPLOADS, json=body_of(), headers={**boris.headers, "Idempotency-Key": key}
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["asset"]["id"] != second.json()["asset"]["id"]
    assert "idempotency-replayed" not in second.headers


# ----------------------------------------------------------------------------- лимиты и сбои
async def test_upload_init_has_its_own_rate_limit(
    test_settings: Settings, jobs: InMemoryJobQueue, storage: InMemoryObjectStorage
) -> None:
    async with limited_client(test_settings, jobs, storage=storage, upload_init=2) as (_, client):
        user = await verified_user(client, jobs)
        assert (await start(client, user)).status_code == 201
        assert (await start(client, user)).status_code == 201
        limited = await start(client, user)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0
    assert limited.headers["ratelimit-limit"] == "2"


async def test_an_unavailable_storage_gives_503_and_no_asset(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    storage.unavailable = True

    response = await start(client, user)

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    assert await fetch_all(admin_engine, "SELECT 1 FROM media.assets") == []


async def test_without_s3_settings_the_upload_routes_answer_503(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with client_with(
        test_settings, jobs
    ) as client:  # хранилище не подставлено, S3_* не заданы
        user = await verified_user(client, jobs)
        response = await start(client, user)
        quota = await client.get("/api/v1/media/quota", headers=bearer(user.auth["access_token"]))
    assert response.status_code == 503
    assert quota.status_code == 200  # квота читается из БД и хранилища не требует

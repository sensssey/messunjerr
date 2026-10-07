"""Медиа на настоящем S3 (SeaweedFS из Compose): подпись ссылок, загрузка, чтение, удаление.

Нужны `S3_ENDPOINT_INTERNAL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (их задаёт Compose для `tools`, в CI
SeaweedFS поднимается отдельным шагом); без них тесты пропускаются. Presigned-ссылки выписываются
на тот же адрес, по которому тест их и использует: подпись включает `Host`. Через Caddy и из
браузера то же самое проверяют тесты стенда и `scripts/stand_browser_check.py`.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.main import create_app
from messunjerr.media.commands import housekeeping
from messunjerr.media.commands.process_media import Outcome, ProcessMedia, process_media
from messunjerr.media.domain.ports import ObjectStorage, PresignedUpload, StorageUnavailableError
from messunjerr.media.infra.s3 import S3ObjectStorage
from messunjerr.media.services import build_storage
from messunjerr.settings import Settings

from .helpers import fetch_one, verified_user
from .media_helpers import JPEG, MEDIA, complete, read_asset, started

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def s3_settings(test_settings: Settings) -> Settings:
    endpoint = os.environ.get("S3_ENDPOINT_INTERNAL")
    access, secret = os.environ.get("S3_ACCESS_KEY"), os.environ.get("S3_SECRET_KEY")
    if not (endpoint and access and secret):
        pytest.skip(
            "нужны S3_ENDPOINT_INTERNAL, S3_ACCESS_KEY, S3_SECRET_KEY (SeaweedFS): запускайте через Compose"
        )
    return test_settings.model_copy(
        update={
            "s3_endpoint_internal": endpoint,
            "s3_endpoint_public": endpoint,  # браузера тут нет: PUT делает сам тест, на тот же адрес
            "s3_access_key": SecretStr(access),
            "s3_secret_key": SecretStr(secret),
            "s3_bucket": os.environ.get("S3_BUCKET", "media"),
        }
    )


@pytest_asyncio.fixture
async def s3(s3_settings: Settings) -> AsyncIterator[ObjectStorage]:
    storage = build_storage(s3_settings)
    try:
        yield storage
    finally:
        await storage.close()


def fresh_key() -> str:
    return f"uploads/pytest-{uuid.uuid4().hex}/original"


async def put(url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as http:
        return await http.put(url, content=body, headers=headers)


async def put_as_issued(presigned: PresignedUpload, body: bytes) -> httpx.Response:
    """`PUT` так, как его делает клиент: на выданную ссылку с выданными заголовками."""
    return await put(presigned.url, body, presigned.headers)


# ----------------------------------------------------------------------------- клиент S3
async def test_a_presigned_put_stores_the_object_and_head_and_read_see_it(
    s3: ObjectStorage,
) -> None:
    key = fresh_key()
    body = JPEG + b"\x01" * 5000
    try:
        presigned = await s3.presign_put(
            key=key, content_type="image/jpeg", content_length=len(body), expires_in=120
        )
        response = await put_as_issued(presigned, body)
        assert response.status_code == 200, response.text

        stored = await s3.head(key)
        assert stored is not None
        assert (stored.size, stored.content_type) == (len(body), "image/jpeg")
        assert await s3.read_head(key, 16) == body[:16]
        assert len(await s3.read_head(key, 4096) or b"") == 4096
    finally:
        await s3.delete_many([key])


@pytest.mark.parametrize(
    ("content_type", "body_size", "why"),
    [
        ("text/plain", 100, "другой тип"),
        ("image/jpeg", 101, "другой размер"),
        ("image/jpeg", 99, "другой размер"),
    ],
)
async def test_the_signature_pins_the_type_and_the_exact_size(
    s3: ObjectStorage, content_type: str, body_size: int, why: str
) -> None:
    key = fresh_key()
    presigned = await s3.presign_put(
        key=key, content_type="image/jpeg", content_length=100, expires_in=120
    )
    try:
        response = await put(
            presigned.url,
            b"\x00" * body_size,
            {**presigned.headers, "Content-Type": content_type},
        )
        assert response.status_code == 403, f"{why}: {response.status_code} {response.text}"
        assert await s3.head(key) is None
    finally:
        await s3.delete_many([key])


async def test_the_link_creates_the_object_once_and_a_second_put_gets_412(
    s3: ObjectStorage,
) -> None:
    key = fresh_key()
    presigned = await s3.presign_put(
        key=key, content_type="image/jpeg", content_length=5, expires_in=120
    )
    try:
        assert (await put_as_issued(presigned, b"first")).status_code == 200
        second = await put_as_issued(presigned, b"other")  # тот же размер, другое содержимое
        assert second.status_code == 412, second.text
        assert "PreconditionFailed" in second.text
        assert await s3.read_head(key, 16) == b"first"  # проверенное содержимое подменить нельзя

        # Условие подписано: без заголовка подпись не сходится, обойти запись один раз нельзя.
        bare = {"Content-Type": "image/jpeg"}
        assert (await put(presigned.url, b"third", bare)).status_code == 403
        assert await s3.read_head(key, 16) == b"first"
    finally:
        await s3.delete_many([key])


async def test_a_slow_second_put_cannot_overwrite_the_first_one(s3: ObjectStorage) -> None:
    """Одновременная запись: условие проверяется и при завершении, не только в начале запроса."""
    key = fresh_key()
    presigned = await s3.presign_put(
        key=key, content_type="image/jpeg", content_length=10, expires_in=120
    )
    gate = asyncio.Event()

    async def slow_body() -> AsyncIterator[bytes]:
        yield b"AAAAA"
        await gate.wait()  # остаток тела клиент задерживает, пока первый PUT не завершится
        yield b"AAAAA"

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            late = asyncio.create_task(
                http.put(
                    presigned.url,
                    content=slow_body(),
                    headers={**presigned.headers, "Content-Length": "10"},
                )
            )
            await asyncio.sleep(1.0)
            first = await http.put(presigned.url, content=b"BBBBBBBBBB", headers=presigned.headers)
            gate.set()
            second = await late
        assert (first.status_code, second.status_code) == (200, 412), second.text
        assert await s3.read_head(key, 16) == b"BBBBBBBBBB"  # проверенное содержимое осталось
    finally:
        await s3.delete_many([key])


async def test_an_expired_link_is_refused(s3: ObjectStorage) -> None:
    key = fresh_key()
    presigned = await s3.presign_put(
        key=key, content_type="image/jpeg", content_length=10, expires_in=1
    )
    await asyncio.sleep(2.5)
    try:
        assert (await put_as_issued(presigned, b"\x00" * 10)).status_code == 403
        assert await s3.head(key) is None
    finally:
        await s3.delete_many([key])


async def test_a_link_for_one_key_does_not_write_another(s3: ObjectStorage) -> None:
    key, other = fresh_key(), fresh_key()
    presigned = await s3.presign_put(
        key=key, content_type="image/jpeg", content_length=10, expires_in=60
    )
    try:
        response = await put(presigned.url.replace(key, other), b"\x00" * 10, presigned.headers)
        assert response.status_code == 403
        assert await s3.head(other) is None
    finally:
        await s3.delete_many([key, other])


async def test_missing_objects_are_not_errors_for_head_read_and_delete(s3: ObjectStorage) -> None:
    key = fresh_key()
    assert await s3.head(key) is None
    assert await s3.read_head(key, 16) is None
    await s3.delete_many([key])  # повтор безопасен


async def test_delete_many_removes_every_key(s3: ObjectStorage) -> None:
    keys = [fresh_key() for _ in range(5)]
    for key in keys:
        presigned = await s3.presign_put(
            key=key, content_type="image/jpeg", content_length=3, expires_in=60
        )
        assert (await put_as_issued(presigned, b"abc")).status_code == 200
    await s3.delete_many(keys)
    for key in keys:
        assert await s3.head(key) is None


async def test_an_unreachable_endpoint_is_unavailable_not_a_crash(s3_settings: Settings) -> None:
    broken = build_storage(
        s3_settings.model_copy(update={"s3_endpoint_internal": "http://127.0.0.1:9"})
    )
    try:
        with pytest.raises(StorageUnavailableError):
            await broken.head(fresh_key())
    finally:
        await broken.close()


async def test_wrong_keys_are_reported_as_unavailable(s3_settings: Settings) -> None:
    wrong = build_storage(
        s3_settings.model_copy(update={"s3_secret_key": SecretStr("not-the-secret")})
    )
    try:
        with pytest.raises(StorageUnavailableError):
            await wrong.head(fresh_key())
    finally:
        await wrong.close()


# ----------------------------------------------------------------------------- вся цепочка через API
async def test_the_whole_chain_with_the_real_storage(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    s3_settings: Settings,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    storage = build_storage(s3_settings)
    application = create_app(s3_settings, job_queue=jobs, storage=storage)
    try:
        async with LifespanManager(application):
            transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
                body = JPEG + b"\x07" * 3000
                created = await started(api, user, size_bytes=len(body))
                upload = created["upload"]
                asset_id = created["asset"]["id"]

                put_response = await put(upload["url"], body, upload["headers"])
                assert put_response.status_code == 200, put_response.text

                done = await complete(api, user, asset_id)
                assert done.status_code == 202, done.text
                assert done.json()["asset"]["status"] == "uploaded"

                outcome = await process_media(
                    ProcessMedia(uuid.UUID(asset_id)),
                    sessionmaker=sessionmaker,
                    storage=storage,
                    jobs=jobs,
                )
                assert outcome is Outcome.READY
                ready = (await read_asset(api, user, asset_id)).json()
                assert (ready["status"], ready["content_type"]) == ("ready", "image/jpeg")

                key = f"uploads/{asset_id}/original"
                assert (await storage.head(key)) is not None
                deleted = await api.delete(f"{MEDIA}/{asset_id}", headers=user.headers)
                assert deleted.status_code == 204
                removed = await housekeeping.delete_media_objects(
                    sessionmaker, storage, [uuid.UUID(asset_id)], link_lifetime=timedelta(0)
                )
                assert removed == 1
                assert await storage.head(key) is None
                row: dict[str, Any] = await fetch_one(
                    admin_engine,
                    "SELECT objects_deleted_at FROM media.assets WHERE id = :id",
                    id=uuid.UUID(asset_id),
                )
                assert row["objects_deleted_at"] is not None
    finally:
        await storage.close()


async def test_complete_reports_503_when_the_storage_does_not_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, s3_settings: Settings
) -> None:
    user = await verified_user(client, jobs)
    dead = s3_settings.model_copy(update={"s3_endpoint_internal": "http://127.0.0.1:9"})
    application = create_app(dead, job_queue=jobs)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            created = await started(api, user)  # ссылка подписывается локально и от сети не зависит
            response = await complete(api, user, created["asset"]["id"])
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


# ----------------------------------------------------------------------------- удаление и «живая» ссылка
async def test_an_object_recreated_through_a_live_link_after_deletion_is_swept_later(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    s3_settings: Settings,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    """Условная запись не мешает создать объект заново после удаления: такой объект никто не учитывает.

    Пока ссылка жива, удаление повторяется, а строка закрывается отметкой лишь после её срока.
    """
    user = await verified_user(client, jobs)
    storage = build_storage(s3_settings)
    lifetime = timedelta(seconds=s3_settings.upload_url_ttl_seconds + 60)
    application = create_app(s3_settings, job_queue=jobs, storage=storage)
    try:
        async with LifespanManager(application):
            transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
                body = JPEG + b"\x05" * 500
                created = await started(api, user, size_bytes=len(body))
                upload = created["upload"]
                asset_id = uuid.UUID(created["asset"]["id"])
                key = f"uploads/{asset_id}/original"
                assert (await put(upload["url"], body, upload["headers"])).status_code == 200

                deleted = await api.delete(f"{MEDIA}/{asset_id}", headers=user.headers)
                assert deleted.status_code == 204
                first_pass = await housekeeping.delete_media_objects(
                    sessionmaker, storage, [asset_id], link_lifetime=lifetime
                )
                assert first_pass == 1
                assert await storage.head(key) is None

                # Клиент, не потерявший ссылку, кладёт объект снова: хранилище это разрешает.
                again = await put(upload["url"], body, upload["headers"])
                assert again.status_code == 200, again.text
                assert await storage.head(key) is not None
                row: dict[str, Any] = await fetch_one(
                    admin_engine,
                    "SELECT objects_deleted_at FROM media.assets WHERE id = :id",
                    id=asset_id,
                )
                assert row["objects_deleted_at"] is None  # окно ссылки ещё открыто

                late = utcnow() + lifetime + timedelta(seconds=5)
                second_pass = await housekeeping.delete_media_objects(
                    sessionmaker, storage, [asset_id], link_lifetime=lifetime, now=late
                )
                assert second_pass == 1
                assert await storage.head(key) is None
                row = await fetch_one(
                    admin_engine,
                    "SELECT objects_deleted_at FROM media.assets WHERE id = :id",
                    id=asset_id,
                )
                assert row["objects_deleted_at"] is not None
    finally:
        await storage.close()


# ----------------------------------------------------------------------------- листинг и сверка объектов
def unique_ids(count: int) -> tuple[str, list[uuid.UUID]]:
    """Идентификаторы с общим префиксом: тест сверки видит только свои объекты в общем bucket."""
    head = uuid.uuid4().hex[:8]
    return head, [uuid.UUID(f"{head}-0000-4000-8000-{n:012d}") for n in range(count)]


async def test_listing_follows_the_continuation_token_and_reports_modification_times(
    s3_settings: Settings,
) -> None:
    head, ids = unique_ids(5)
    keys = [f"uploads/{asset_id}/original" for asset_id in ids]
    paged = S3ObjectStorage(
        internal_endpoint=str(s3_settings.s3_endpoint_internal),
        public_endpoint=s3_settings.storage_public_url,
        bucket=s3_settings.s3_bucket,
        access_key=s3_settings.s3_access_key.get_secret_value()
        if s3_settings.s3_access_key
        else "",
        secret_key=s3_settings.s3_secret_key.get_secret_value()
        if s3_settings.s3_secret_key
        else "",
        page_size=2,
    )
    try:
        for key in keys:
            presigned = await paged.presign_put(
                key=key, content_type="image/jpeg", content_length=3, expires_in=60
            )
            assert (await put_as_issued(presigned, b"abc")).status_code == 200

        pages = [page async for page in paged.list_objects(f"uploads/{head}")]

        assert [len(page) for page in pages] == [2, 2, 1]  # три страницы по продолжению
        listed = [item for page in pages for item in page]
        assert [item.key for item in listed] == sorted(keys)
        assert {item.size for item in listed} == {3}
        assert all(item.modified_at.tzinfo is not None for item in listed)
        newest = max(item.modified_at for item in listed)
        assert abs((utcnow() - newest).total_seconds()) < 120  # время записи, а не эпоха
    finally:
        await paged.delete_many(keys)
        await paged.close()


async def test_the_sweep_removes_orphans_from_the_real_storage_and_keeps_the_rest(
    s3: ObjectStorage, sessionmaker: Sessions, client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    head, ids = unique_ids(3)
    keys = [f"uploads/{asset_id}/original" for asset_id in ids]
    for key in keys:
        presigned = await s3.presign_put(
            key=key, content_type="image/jpeg", content_length=3, expires_in=60
        )
        assert (await put_as_issued(presigned, b"abc")).status_code == 200
    try:
        later = utcnow() + timedelta(minutes=1)

        result = await housekeeping.sweep_orphan_objects(
            sessionmaker, s3, older_than=timedelta(0), prefix=f"uploads/{head}", now=later
        )

        assert (result.scanned, result.removed) == (3, 3)
        assert [page async for page in s3.list_objects(f"uploads/{head}")] == []
    finally:
        await s3.delete_many(keys)


async def test_a_wrong_bucket_is_unavailability_not_a_missing_file(s3_settings: Settings) -> None:
    wrong = build_storage(
        s3_settings.model_copy(update={"s3_bucket": f"no-such-bucket-{uuid.uuid4().hex[:8]}"})
    )
    try:
        with pytest.raises(StorageUnavailableError):
            await wrong.read_head(fresh_key(), 16)
        with pytest.raises(StorageUnavailableError):
            await wrong.delete_many([fresh_key()])
    finally:
        await wrong.close()

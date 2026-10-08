"""Хранилище медиа без сети: подпись presigned PUT, подставное хранилище, настройки S3."""

import asyncio
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from messunjerr.media.domain.ports import PresignedUpload, StorageUnavailableError
from messunjerr.media.infra.memory import InMemoryObjectStorage, UnconfiguredStorage
from messunjerr.media.infra.s3 import (
    Deadlines,
    S3ObjectStorage,
    is_missing_object,
    is_unsatisfiable_range,
)
from messunjerr.media.services import build_storage
from messunjerr.settings import Settings, check_runtime


def settings_with(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr("postgresql+asyncpg://app:p@h/db"),
        "redis_url": SecretStr("redis://h/0"),
        # Явные None перекрывают переменные окружения контейнера (в нём бывают настоящие ключи S3).
        "s3_endpoint_internal": None,
        "s3_endpoint_public": None,
        "s3_access_key": None,
        "s3_secret_key": None,
    }
    values.update(overrides)
    return Settings(**values)  # pyright: ignore[reportCallIssue]


def new_storage(**overrides: Any) -> S3ObjectStorage:
    options: dict[str, Any] = {
        "internal_endpoint": "http://seaweedfs:8333",
        "public_endpoint": "https://messunjerr.localhost",
        "bucket": "media",
        "access_key": "AKIA_TEST",
        "secret_key": "secret-test",
        "region": "us-east-1",
    }
    options.update(overrides)
    return S3ObjectStorage(**options)


# ----------------------------------------------------------------------------- presigned PUT
async def test_presigned_put_is_signed_for_the_public_address_in_path_style() -> None:
    storage = new_storage()
    key = f"uploads/{uuid.uuid4()}/original"
    try:
        presigned = await storage.presign_put(
            key=key, content_type="image/jpeg", content_length=1234, expires_in=900
        )
    finally:
        await storage.close()
    parts = urlsplit(presigned.url)
    assert (parts.scheme, parts.netloc) == ("https", "messunjerr.localhost")
    assert parts.path == f"/media/{key}"  # bucket первым сегментом, как проксирует Caddy
    query = parse_qs(parts.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert "X-Amz-Signature" in query


async def test_presigned_put_pins_the_type_the_exact_size_and_the_create_once_rule() -> None:
    storage = new_storage()
    try:
        presigned = await storage.presign_put(
            key="uploads/x/original", content_type="image/png", content_length=77, expires_in=60
        )
    finally:
        await storage.close()
    signed = parse_qs(urlsplit(presigned.url).query)["X-Amz-SignedHeaders"][0].split(";")
    assert signed == ["content-length", "content-type", "host", "if-none-match"]
    # Клиент обязан прислать ровно то, что подписано (кроме длины: её ставит сам браузер).
    assert presigned.headers == {"Content-Type": "image/png", "If-None-Match": "*"}


async def test_presigned_urls_differ_by_size_and_type() -> None:
    storage = new_storage()
    try:
        first = await storage.presign_put(
            key="uploads/x/original", content_type="image/png", content_length=10, expires_in=60
        )
        other_size = await storage.presign_put(
            key="uploads/x/original", content_type="image/png", content_length=11, expires_in=60
        )
        other_type = await storage.presign_put(
            key="uploads/x/original", content_type="image/jpeg", content_length=10, expires_in=60
        )
    finally:
        await storage.close()

    def signature(presigned: PresignedUpload) -> str:
        return parse_qs(urlsplit(presigned.url).query)["X-Amz-Signature"][0]

    assert len({signature(first), signature(other_size), signature(other_type)}) == 3


async def test_closing_an_unused_storage_is_harmless_and_repeatable() -> None:
    storage = new_storage()
    await storage.close()
    await storage.close()


async def test_an_unreachable_endpoint_is_reported_as_unavailable() -> None:
    storage = new_storage(internal_endpoint="http://127.0.0.1:9")  # порт 9 закрыт
    try:
        with pytest.raises(StorageUnavailableError):
            await storage.head("uploads/x/original")
        with pytest.raises(StorageUnavailableError):
            await storage.read_head("uploads/x/original", 16)
        with pytest.raises(StorageUnavailableError):
            await storage.delete_many(["uploads/x/original"])
    finally:
        await storage.close()


async def test_a_storage_that_accepts_connections_but_never_answers_is_unavailable_in_time() -> (
    None
):
    """Зависший ответ (остановленное хранилище, потерянные пакеты) не должен вешать запрос человека."""

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()  # молчим, пока клиент сам не закроет соединение
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    storage = new_storage(
        internal_endpoint=f"http://127.0.0.1:{port}",
        deadlines=Deadlines(head=0.4, read=0.4, delete=0.4),
    )
    started = time.monotonic()
    try:
        with pytest.raises(StorageUnavailableError):
            await storage.head("uploads/x/original")
        with pytest.raises(StorageUnavailableError):
            await storage.read_head("uploads/x/original", 16)
        with pytest.raises(StorageUnavailableError):
            await storage.delete_many(["uploads/x/original", "uploads/y/original"])
        assert time.monotonic() - started < 5
    finally:
        await storage.close()
        server.close()
        await server.wait_closed()


# ----------------------------------------------------------------------------- подставное хранилище
async def test_the_in_memory_storage_behaves_like_s3_for_the_commands() -> None:
    storage = InMemoryObjectStorage()
    assert await storage.head("k") is None
    assert await storage.read_head("k", 4) is None
    storage.put("k", b"abcdefgh", "image/png")
    head = await storage.head("k")
    assert head is not None
    assert (head.size, head.content_type) == (8, "image/png")
    assert await storage.read_head("k", 3) == b"abc"
    await storage.delete_many(["k", "never-existed"])  # отсутствующий ключ не ошибка
    assert await storage.head("k") is None
    assert storage.deleted == ["k", "never-existed"]


async def test_the_in_memory_storage_signs_like_s3_and_creates_objects_once() -> None:
    storage = InMemoryObjectStorage()
    presigned = await storage.presign_put(
        key="uploads/x/original", content_type="image/png", content_length=3, expires_in=60
    )
    assert presigned.headers == {"Content-Type": "image/png", "If-None-Match": "*"}
    assert presigned.url.startswith("http://storage.test/media/uploads/x/original?")

    assert storage.put_once("uploads/x/original", b"abc", "image/png") is True
    assert storage.put_once("uploads/x/original", b"xyz", "image/png") is False  # в S3 это 412
    assert (await storage.read_head("uploads/x/original", 8)) == b"abc"


def client_error(code: str, status: int) -> ClientError:
    response: Any = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(response, "GetObject")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_a_missing_key_is_not_an_error(code: str) -> None:
    assert is_missing_object(client_error(code, 404))


@pytest.mark.parametrize(
    ("code", "status"),
    [("NoSuchBucket", 404), ("AccessDenied", 403), ("InternalError", 500), ("SlowDown", 503)],
)
def test_a_missing_bucket_or_a_failure_is_not_a_missing_key(code: str, status: int) -> None:
    # Опечатка в имени bucket не должна выглядеть как «файла нет»: complete отправил бы человека
    # загружать файл заново, а очистка сочла бы объекты убранными.
    assert not is_missing_object(client_error(code, status))


@pytest.mark.parametrize(("code", "status"), [("InvalidRange", 416), ("Whatever", 416)])
def test_a_range_of_an_empty_object_is_recognised(code: str, status: int) -> None:
    assert is_unsatisfiable_range(client_error(code, status))


@pytest.mark.parametrize(
    ("code", "status"), [("NoSuchKey", 404), ("AccessDenied", 403), ("InternalError", 500)]
)
def test_other_errors_are_not_taken_for_an_empty_object(code: str, status: int) -> None:
    assert not is_unsatisfiable_range(client_error(code, status))


async def test_warming_up_creates_the_clients_without_touching_the_network() -> None:
    storage = new_storage(internal_endpoint="http://127.0.0.1:9")  # порт 9 закрыт
    try:
        await storage.warm_up()  # сеть не нужна
        await storage.warm_up()  # повтор безопасен
    finally:
        await storage.close()
    await InMemoryObjectStorage().warm_up()
    await UnconfiguredStorage().warm_up()


async def test_the_in_memory_storage_lists_objects_by_prefix_in_pages() -> None:
    storage = InMemoryObjectStorage(page_size=2)
    for key in ("uploads/a/original", "uploads/b/original", "uploads/c/original", "public/x"):
        storage.put(key, b"12345")

    pages = [page async for page in storage.list_objects("uploads/")]

    assert [[item.key for item in page] for page in pages] == [
        ["uploads/a/original", "uploads/b/original"],
        ["uploads/c/original"],
    ]
    assert all(
        item.size == 5 and item.modified_at.tzinfo is not None for page in pages for item in page
    )


async def test_listing_follows_the_outage_switch_and_the_unconfigured_storage_refuses() -> None:
    down = InMemoryObjectStorage(unavailable=True)
    with pytest.raises(StorageUnavailableError):
        async for _ in down.list_objects("uploads/"):
            pass
    with pytest.raises(StorageUnavailableError):
        async for _ in UnconfiguredStorage().list_objects("uploads/"):
            pass


async def test_the_in_memory_storage_can_imitate_an_outage() -> None:
    storage = InMemoryObjectStorage(unavailable=True)
    for call in (
        storage.head("k"),
        storage.read_head("k", 1),
        storage.delete_many(["k"]),
        storage.presign_put(key="k", content_type="x/y", content_length=1, expires_in=1),
    ):
        with pytest.raises(StorageUnavailableError):
            await call


async def test_the_unconfigured_storage_answers_unavailable() -> None:
    storage = UnconfiguredStorage()
    with pytest.raises(StorageUnavailableError):
        await storage.head("k")
    await storage.close()


# ----------------------------------------------------------------------------- настройки
def test_without_s3_settings_the_app_gets_the_unconfigured_storage() -> None:
    assert isinstance(build_storage(settings_with()), UnconfiguredStorage)


def test_with_s3_settings_the_app_gets_the_s3_storage() -> None:
    settings = settings_with(
        s3_endpoint_internal="http://seaweedfs:8333/",
        s3_access_key=SecretStr("a"),
        s3_secret_key=SecretStr("b"),
    )
    assert settings.s3_endpoint_internal == "http://seaweedfs:8333"  # слэш на конце убран
    assert settings.storage_configured
    assert isinstance(build_storage(settings), S3ObjectStorage)


def test_the_public_address_defaults_to_the_site_address() -> None:
    assert settings_with(public_base_url="https://messunjerr.localhost/").storage_public_url == (
        "https://messunjerr.localhost"
    )
    explicit = settings_with(
        public_base_url="https://messunjerr.localhost", s3_endpoint_public="http://localhost:8333"
    )
    assert explicit.storage_public_url == "http://localhost:8333"


def test_empty_endpoints_mean_not_set_and_bad_ones_are_refused() -> None:
    assert settings_with(s3_endpoint_internal="").s3_endpoint_internal is None
    with pytest.raises(ValueError, match="http"):
        settings_with(s3_endpoint_internal="seaweedfs:8333")


def test_production_refuses_to_start_without_the_storage_but_dev_does_not() -> None:
    prod = settings_with(
        app_env="prod", public_base_url="https://example.ru", jwt_private_key=SecretStr("k")
    )
    check_runtime(prod)  # без требования хранилища проверка проходит
    with pytest.raises(RuntimeError, match="S3_ENDPOINT_INTERNAL"):
        check_runtime(prod, needs_storage=True)
    check_runtime(settings_with(app_env="dev"), needs_storage=True)


def test_only_the_api_needs_the_token_signing_key() -> None:
    """Воркеры, особенно media (там разбираются недоверенные файлы), ключ подписи токенов не получают."""
    without_key = settings_with(
        app_env="stage",
        public_base_url="https://messunjerr.localhost",
        jwt_private_key=None,  # явно: иначе Settings возьмёт ключ из окружения контейнера
        s3_endpoint_internal="http://seaweedfs:8333",
        s3_access_key=SecretStr("a"),
        s3_secret_key=SecretStr("b"),
    )
    assert without_key.jwt_private_key is None

    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY"):  # API: по умолчанию ключ обязателен
        check_runtime(without_key, needs_storage=True)
    check_runtime(
        without_key, needs_storage=True, needs_jwt=False
    )  # воркер media стартует без него


def test_production_with_the_storage_configured_starts() -> None:
    settings = settings_with(
        app_env="stage",
        public_base_url="https://messunjerr.localhost",
        jwt_private_key=SecretStr("k"),
        s3_endpoint_internal="http://seaweedfs:8333",
        s3_access_key=SecretStr("a"),
        s3_secret_key=SecretStr("b"),
    )
    check_runtime(settings, needs_storage=True)

"""Обработка изображений и файлов (S6-01, S6-02, S6-08) на настоящей БД с подставным хранилищем.

Проверяется весь путь воркера: сигнатура, разбор Pillow, варианты, запись в хранилище, итог в БД,
замена оригинала пустым объектом, отказы с причинами и сбои хранилища посреди работы.
"""

import io
import struct
import uuid
import zlib
from typing import Any

import httpx
import pytest
from PIL import Image
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.me import avatar_for
from messunjerr.media.commands import process_media as process_module
from messunjerr.media.commands.process_media import Outcome
from messunjerr.media.commands.reprocess import reprocess_legacy_images
from messunjerr.media.domain.ports import StorageUnavailableError
from messunjerr.media.domain.rules import PUBLIC_CACHE_CONTROL
from messunjerr.media.infra.images import DecodeBudget, ImageRejectedError
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.settings import Settings

from .helpers import execute, fetch_all, fetch_one, verified_user
from .media_helpers import (
    EXE,
    GIF,
    JPEG,
    PDF,
    PNG,
    SVG,
    WEBP,
    encode,
    gps_exif,
    key_of,
    process,
    put_object,
    quadrants,
    read_asset,
    started,
    uploaded,
)

Sessions = async_sessionmaker[AsyncSession]


async def row_of(engine: AsyncEngine, asset_id: str) -> dict[str, Any]:
    return await fetch_one(
        engine, "SELECT * FROM media.assets WHERE id = :id", id=uuid.UUID(asset_id)
    )


async def events_of(engine: AsyncEngine) -> list[str]:
    rows = await fetch_all(
        engine, "SELECT event_type FROM platform.outbox WHERE topic = 'mj.media.v1' ORDER BY id"
    )
    return [row["event_type"] for row in rows]


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def png_bomb(width: int, height: int) -> bytes:
    """Однобитный PNG: заголовок называет огромный растр, а сжатые нули занимают килобайты."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload))
        )

    row = b"\x00" * (1 + (width + 7) // 8)
    compressor = zlib.compressobj(9)
    stream = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", stream) + chunk(b"IEND", b"")
    )


def photo_with_secrets() -> bytes:
    return encode(quadrants((640, 480)), "JPEG", exif=gps_exif(), quality=90)


def jpeg_with_many_scans() -> bytes:
    """Прогрессивный снимок, где последний скан заменён сотнями пустых: на разбор уходят секунды."""
    source = encode(quadrants((320, 240)), "JPEG", quality=80, progressive=True)
    last = source.rindex(b"\xff\xda")
    length = struct.unpack(">H", source[last + 2 : last + 4])[0]
    return (
        source[:last]
        + source[last : last + 2 + length] * 300
        + source[source.rindex(b"\xff\xd9") :]
    )


def png_with_many_chunks() -> bytes:
    """Картинка 8×8, перед данными 100 000 пустых приватных чанков (файл больше мегабайта)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 8, 8, 8, 0, 0, 0, 0)
    rows = zlib.compress(b"".join(b"\x00" + bytes(8) for _ in range(8)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"prVt", b"") * 100_000
        + chunk(b"IDAT", rows)
        + chunk(b"IEND", b"")
    )


def gif_with_many_blocks() -> bytes:
    """GIF 8×8, перед кадром 100 000 блоков-комментариев."""
    source = encode(Image.new("P", (8, 8), 0), "GIF")
    table_end = 13 + 3 * 2 ** ((source[10] & 7) + 1)  # заголовок и глобальная таблица цветов
    return source[:table_end] + b"\x21\xfe\x01A\x00" * 100_000 + source[table_end:]


# ----------------------------------------------------------------------------- фото
async def test_a_photo_becomes_webp_variants_without_metadata_and_the_original_is_emptied(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    source = photo_with_secrets()
    created = await uploaded(client, user, storage, source, content_type="image/jpeg")
    asset_id = created["asset"]["id"]
    assert b"SecretModel" in storage.objects[key_of(created)][0]

    outcome = await process(asset_id, sessionmaker, storage, jobs)

    assert outcome is Outcome.READY
    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["content_type"], row["reject_reason"]) == (
        "ready",
        "image/webp",
        None,
    )
    assert (row["width"], row["height"]) == (
        640,
        480,
    )  # размеры большего варианта; вверх не растягивается
    thumb_key, medium_key = f"uploads/{asset_id}/thumb.webp", f"uploads/{asset_id}/medium.webp"
    assert row["variants"]["thumb"]["key"] == thumb_key
    assert row["variants"]["medium"]["key"] == medium_key
    assert (row["variants"]["thumb"]["width"], row["variants"]["thumb"]["height"]) == (320, 240)
    for key in (thumb_key, medium_key):
        body, content_type = storage.objects[key]
        assert content_type == "image/webp"
        assert b"Secret" not in body
        assert b"Exif" not in body
        assert storage.cache_control.get(key) is None  # закрытые варианты в кэше надолго не нужны
    # Оригинал с геометкой заменён пустым объектом: ключ занят, ссылка на загрузку дальше не пустит.
    assert storage.objects[key_of(created)][0] == b""
    assert storage.put_once(key_of(created), b"swapped", "image/jpeg") is False
    assert row["size_bytes"] == sum(len(storage.objects[key][0]) for key in (thumb_key, medium_key))
    assert await events_of(admin_engine) == ["AssetUploaded", "AssetProcessed"]
    assert jobs.named("delete_media_objects") == []


async def test_the_card_of_a_ready_photo_carries_fresh_presigned_links(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    test_settings: Settings,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    asset_id = created["asset"]["id"]
    assert (await read_asset(client, user, asset_id)).json()["urls"] == {
        "thumb": None,
        "medium": None,
        "original": None,
    }
    await process(asset_id, sessionmaker, storage, jobs)

    card = (await read_asset(client, user, asset_id)).json()

    assert card["status"] == "ready"
    assert card["content_type"] == "image/webp"
    assert card["urls"]["thumb"].startswith("http://storage.test/media/uploads/")
    assert f"{asset_id}/thumb.webp" in card["urls"]["thumb"]
    assert f"{asset_id}/medium.webp" in card["urls"]["medium"]
    assert card["urls"]["original"] is None
    assert card["url_expires_at"] is not None
    gets = {request.key: request for request in storage.presigned_gets}
    medium = gets[f"uploads/{asset_id}/medium.webp"]
    assert medium.expires_in == test_settings.download_url_ttl_seconds == 600
    assert medium.content_type == "image/webp"  # заголовок ответа задаёт ссылка, а не метаданные


# ----------------------------------------------------------------------------- аватар
async def test_an_avatar_is_published_under_the_public_prefix_with_a_year_of_cache(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(
        client, user, storage, photo_with_secrets(), purpose="avatar", content_type="image/jpeg"
    )
    asset_id = created["asset"]["id"]

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY

    small, large = f"public/avatars/{asset_id}/64.webp", f"public/avatars/{asset_id}/256.webp"
    for key in (small, large):
        body, content_type = storage.objects[key]
        assert content_type == "image/webp"
        assert storage.cache_control[key] == PUBLIC_CACHE_CONTROL
        assert b"Secret" not in body
    assert Image.open(io.BytesIO(storage.objects[small][0])).size == (64, 64)
    assert Image.open(io.BytesIO(storage.objects[large][0])).size == (256, 256)
    assert storage.objects[key_of(created)][0] == b""
    row = await row_of(admin_engine, asset_id)
    assert (row["width"], row["height"]) == (256, 256)

    card = (await read_asset(client, user, asset_id)).json()
    base = f"{test_settings.storage_public_url}/{test_settings.s3_bucket}"
    assert card["urls"] == {
        "thumb": f"{base}/{small}",
        "medium": f"{base}/{large}",
        "original": None,
    }
    assert card["url_expires_at"] is None  # адрес постоянный
    # Адреса из профиля и карточки ресурса указывают на одни и те же объекты.
    avatar = avatar_for(uuid.UUID(asset_id))
    assert avatar is not None
    assert (avatar.sm, avatar.md) == (
        f"/{test_settings.s3_bucket}/{small}",
        f"/{test_settings.s3_bucket}/{large}",
    )


async def test_group_avatars_are_published_the_same_way(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(
        client, user, storage, PNG, purpose="group_avatar", content_type="image/png"
    )
    asset_id = created["asset"]["id"]

    await process(asset_id, sessionmaker, storage, jobs)

    assert f"public/avatars/{asset_id}/64.webp" in storage.objects
    assert f"public/avatars/{asset_id}/256.webp" in storage.objects


# ----------------------------------------------------------------------------- GIF
async def test_a_gif_keeps_its_original_and_gets_static_variants(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    frames = [
        Image.new("RGB", (60, 40), color) for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))
    ]
    animation = encode(
        frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, animation, content_type="image/gif")
    asset_id = created["asset"]["id"]

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY

    assert (
        storage.objects[key_of(created)][0] == animation
    )  # оригинал на месте: анимацию не трогаем
    row = await row_of(admin_engine, asset_id)
    assert set(row["variants"]) == {"thumb", "medium", "original"}
    assert row["size_bytes"] == (
        len(animation)
        + len(storage.objects[f"uploads/{asset_id}/thumb.webp"][0])
        + len(storage.objects[f"uploads/{asset_id}/medium.webp"][0])
    )
    card = (await read_asset(client, user, asset_id)).json()
    assert card["urls"]["original"] is not None
    assert f"{asset_id}/original" in card["urls"]["original"]
    request = next(r for r in storage.presigned_gets if r.key == key_of(created))
    assert request.content_type == "image/gif"
    variant = Image.open(io.BytesIO(storage.objects[f"uploads/{asset_id}/medium.webp"][0]))
    assert getattr(variant, "n_frames", 1) == 1  # варианты статичные: первый кадр


# ----------------------------------------------------------------------------- файлы
async def test_a_file_is_stored_as_is_and_given_out_as_an_attachment(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(
        client,
        user,
        storage,
        PDF,
        purpose="message",
        filename='Отчёт "итог" 2026.pdf',
        content_type="application/pdf",
    )
    asset_id = created["asset"]["id"]
    assert created["upload"]["headers"]["Content-Type"] == "application/octet-stream"

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY

    row = await row_of(admin_engine, asset_id)
    assert (row["content_type"], row["variants"], row["size_bytes"]) == (
        "application/pdf",
        {},
        len(PDF),
    )
    assert storage.objects[key_of(created)][0] == PDF  # файл не трогаем
    assert storage.writes == []
    card = (await read_asset(client, user, asset_id)).json()
    assert card["urls"]["thumb"] is None
    assert card["urls"]["medium"] is None
    assert card["urls"]["original"] is not None
    request = next(r for r in storage.presigned_gets if r.key == key_of(created))
    assert request.content_type == "application/octet-stream"
    assert request.content_disposition is not None
    assert request.content_disposition.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in request.content_disposition
    assert "%D0%9E%D1%82%D1%87%D1%91%D1%82" in request.content_disposition  # «Отчёт»
    assert '"итог"' not in request.content_disposition  # кавычки из имени не попадают в параметр


# ----------------------------------------------------------------------------- ловушки
@pytest.mark.parametrize(
    ("overrides", "body", "reason"),
    [
        ({"content_type": "image/png"}, SVG, "not_an_image"),  # SVG под видом PNG
        ({"content_type": "image/jpeg"}, b"<html><script>alert(1)</script></html>", "not_an_image"),
        ({"content_type": "image/jpeg"}, JPEG[:200], "not_an_image"),  # обрезанный файл
        ({"content_type": "image/png"}, PNG[:60] + b"\xff" * 40 + PNG[100:], "not_an_image"),
        ({"content_type": "image/png"}, png_bomb(8000, 8000), "decompression_bomb"),
        ({"content_type": "image/png"}, png_bomb(5001, 5000), "image_too_large"),
        # Наводнение мелкими частями: маленький растр, но разбор стоил бы секунды и сотни мегабайт.
        ({"content_type": "image/jpeg"}, jpeg_with_many_scans(), "decompression_bomb"),
        ({"content_type": "image/png"}, png_with_many_chunks(), "decompression_bomb"),
        ({"content_type": "image/gif"}, gif_with_many_blocks(), "decompression_bomb"),
        (
            {"content_type": "image/png"},
            b"BM" + b"\x00" * 4 + b"\x00" * 4 + b"x" * 100,
            "unsupported_format",
        ),
        ({"purpose": "avatar", "content_type": "image/png"}, GIF, "unsupported_format"),
        (
            {
                "purpose": "message",
                "content_type": "application/octet-stream",
                "filename": "tool.dat",
            },
            EXE,
            "forbidden_type",
        ),
    ],
)
async def test_traps_are_rejected_with_a_reason_and_nothing_is_published(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    overrides: dict[str, Any],
    body: bytes,
    reason: str,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, body, **overrides)
    asset_id = created["asset"]["id"]
    before = sample("media_rejected_total", reason=reason)

    outcome = await process(asset_id, sessionmaker, storage, jobs)

    assert outcome is Outcome.REJECTED
    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["reject_reason"], row["variants"]) == ("rejected", reason, {})
    assert storage.writes == []  # ни варианта, ни заглушки
    assert [job.kwargs for job in jobs.named("delete_media_objects")] == [{"asset_ids": [asset_id]}]
    assert await events_of(admin_engine) == ["AssetUploaded", "AssetRejected"]
    assert sample("media_rejected_total", reason=reason) == before + 1
    card = (await read_asset(client, user, asset_id)).json()
    assert (card["status"], card["reject_reason"]) == ("rejected", reason)
    assert card["urls"] == {"thumb": None, "medium": None, "original": None}


async def test_the_declared_type_does_not_matter_only_the_content_does(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(
        client, user, storage, WEBP, content_type="image/png", filename="a.png"
    )
    asset_id = created["asset"]["id"]

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY

    assert (await row_of(admin_engine, asset_id))["content_type"] == "image/webp"


async def test_an_object_that_vanished_is_rejected_as_processing_failed(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    del storage.objects[key_of(created)]

    outcome = await process(created["asset"]["id"], sessionmaker, storage, jobs)

    assert outcome is Outcome.REJECTED
    assert (await row_of(admin_engine, created["asset"]["id"]))[
        "reject_reason"
    ] == "processing_failed"


async def test_a_crash_of_the_renderer_rejects_the_file_instead_of_retrying_forever(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("codec exploded")

    monkeypatch.setattr(process_module, "render_image", broken)
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")

    outcome = await process(created["asset"]["id"], sessionmaker, storage, jobs)

    assert outcome is Outcome.REJECTED
    row = await row_of(admin_engine, created["asset"]["id"])
    assert (row["status"], row["reject_reason"]) == ("rejected", "processing_failed")
    assert len(jobs.named("delete_media_objects")) == 1


async def test_the_renderer_error_taxonomy_reaches_the_card(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from messunjerr.media.domain.rules import RejectReason

    def too_large(*_args: object, **_kwargs: object) -> None:
        raise ImageRejectedError(RejectReason.IMAGE_TOO_LARGE)

    monkeypatch.setattr(process_module, "render_image", too_large)
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")

    await process(created["asset"]["id"], sessionmaker, storage, jobs)

    assert (await read_asset(client, user, created["asset"]["id"])).json()[
        "reject_reason"
    ] == "image_too_large"


# ----------------------------------------------------------------------------- сбои хранилища
class FlakyWrites(InMemoryObjectStorage):
    """Хранилище, у которого запись падает после заданного числа удачных."""

    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self.fail_after = fail_after
        self.calls = 0

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        self.calls += 1
        if self.calls > self.fail_after:
            raise StorageUnavailableError("writes are failing")
        await super().write_object(
            key, body, content_type=content_type, cache_control=cache_control
        )


async def test_a_storage_failure_halfway_leaves_the_asset_processing_and_a_retry_finishes_it(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    flaky = FlakyWrites(fail_after=1)  # первый вариант записан, на втором запись падает
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    flaky.objects.update(storage.objects)  # то же хранилище, но с отказами при записи
    asset_id = created["asset"]["id"]

    with pytest.raises(StorageUnavailableError):
        await process(asset_id, sessionmaker, flaky, jobs)

    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["variants"]) == ("processing", {})  # файл не отклонён
    assert flaky.objects[key_of(created)][0] == JPEG  # оригинал цел: повтор есть откуда делать

    flaky.fail_after = 100
    assert await process(asset_id, sessionmaker, flaky, jobs) is Outcome.READY
    row = await row_of(admin_engine, asset_id)
    assert row["status"] == "ready"
    assert flaky.objects[key_of(created)][0] == b""
    assert {key for key in flaky.objects if "/thumb" in key or "/medium" in key} == {
        f"uploads/{asset_id}/thumb.webp",
        f"uploads/{asset_id}/medium.webp",
    }


async def test_an_unavailable_storage_does_not_reject_the_file(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    storage.unavailable = True

    with pytest.raises(StorageUnavailableError):
        await process(created["asset"]["id"], sessionmaker, storage, jobs)

    row = await row_of(admin_engine, created["asset"]["id"])
    assert (row["status"], row["reject_reason"]) == ("processing", None)
    assert jobs.named("delete_media_objects") == []


class DeletesWhileProcessing(InMemoryObjectStorage):
    """Пока воркер пишет вариант, человек успевает удалить ресурс."""

    def __init__(self, engine: AsyncEngine, asset_id: str) -> None:
        super().__init__()
        self.engine = engine
        self.asset_id = asset_id

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        await super().write_object(
            key, body, content_type=content_type, cache_control=cache_control
        )
        await execute(
            self.engine,
            "UPDATE media.assets SET status = 'deleted', deleted_at = now() WHERE id = :id",
            id=uuid.UUID(self.asset_id),
        )


async def test_variants_written_for_a_deleted_asset_are_removed_again(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(
        client, user, size_bytes=len(JPEG), purpose="avatar", content_type="image/jpeg"
    )
    asset_id = created["asset"]["id"]
    racing = DeletesWhileProcessing(admin_engine, asset_id)
    put_object(racing, created, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'uploaded', uploaded_at = now(), size_bytes = :n WHERE id = :id",
        n=len(JPEG),
        id=uuid.UUID(asset_id),
    )

    outcome = await process(asset_id, sessionmaker, racing, jobs)

    assert outcome is Outcome.SKIPPED
    assert not [
        key for key in racing.objects if key.startswith("public/")
    ]  # публичные файлы убраны
    assert (await row_of(admin_engine, asset_id))["status"] == "deleted"


# ----------------------------------------------------------------------------- память и метрики
async def test_processing_returns_what_it_reserved_in_the_decode_budget(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    budget = DecodeBudget(limit_mb=1)  # меньше любой оценки: всё идёт «в одиночку»
    user = await verified_user(client, jobs)
    first = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    second = await uploaded(client, user, storage, PNG, content_type="image/png")

    outcomes = [
        await process(first["asset"]["id"], sessionmaker, storage, jobs, budget=budget),
        await process(second["asset"]["id"], sessionmaker, storage, jobs, budget=budget),
    ]

    assert outcomes == [Outcome.READY, Outcome.READY]
    assert budget.used_mb == 0


async def test_processing_is_measured(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
) -> None:
    ready = {"kind": "image", "outcome": "ready"}
    before = sample("media_processing_seconds_count", **ready)
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")

    await process(created["asset"]["id"], sessionmaker, storage, jobs)

    assert sample("media_processing_seconds_count", **ready) == before + 1
    assert sample("media_processing_seconds_sum", **ready) > 0


async def test_quota_counts_what_is_stored_not_what_was_declared(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    big = encode(quadrants((1600, 1200)), "JPEG", quality=100, subsampling=0)
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, big, content_type="image/jpeg")
    quota = "/api/v1/media/quota"
    assert (await client.get(quota, headers=user.headers)).json()["used_bytes"] == len(big)

    await process(created["asset"]["id"], sessionmaker, storage, jobs)

    stored = (await row_of(admin_engine, created["asset"]["id"]))["size_bytes"]
    assert stored < len(big)
    after = (await client.get(quota, headers=user.headers)).json()
    assert (after["used_bytes"], after["assets_count"]) == (stored, 1)


# ----------------------------------------------------------------------------- ресурсы времён S5
async def test_a_ready_image_without_variants_is_returned_to_processing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, photo_with_secrets(), content_type="image/jpeg")
    asset_id = created["asset"]["id"]
    # Как после заглушки S5: `ready`, тип по сигнатуре, вариантов нет.
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready', content_type = 'image/jpeg', "
        "processed_at = now(), uploaded_at = now() - interval '3 days' WHERE id = :id",
        id=uuid.UUID(asset_id),
    )
    jobs.clear()

    assert await reprocess_legacy_images(sessionmaker, jobs) == 1
    assert await reprocess_legacy_images(sessionmaker, jobs) == 0  # повтор ничего не находит

    row = await row_of(admin_engine, asset_id)
    assert row["status"] == "uploaded"
    assert row["processed_at"] is None
    assert row["uploaded_at"].year >= 2026  # заново «загружен»: суточная очистка его не заденет
    assert [job.kwargs for job in jobs.named("process_media")] == [{"asset_id": asset_id}]

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY
    assert set((await row_of(admin_engine, asset_id))["variants"]) == {"thumb", "medium"}
    assert storage.objects[key_of(created)][0] == b""


async def test_reprocessing_leaves_other_assets_alone(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    pending = await started(client, user)
    processed = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    await process(processed["asset"]["id"], sessionmaker, storage, jobs)
    document = await uploaded(
        client,
        user,
        storage,
        PDF,
        purpose="message",
        content_type="application/pdf",
        filename="a.pdf",
    )
    await process(document["asset"]["id"], sessionmaker, storage, jobs)

    assert await reprocess_legacy_images(sessionmaker, jobs) == 0

    rows = await fetch_all(admin_engine, "SELECT status FROM media.assets ORDER BY created_at")
    assert [row["status"] for row in rows] == ["pending", "ready", "ready"]
    assert pending["asset"]["status"] == "pending"


# ----------------------------------------------------------------------------- дубли и «ядовитые» файлы
class FinishesFirst(InMemoryObjectStorage):
    """Пока копия B пишет варианты, копия A успевает довести тот же ресурс до `ready`.

    Так выглядит дубль задачи после потери ключа в Redis: у обеих копий одни и те же ключи объектов.
    """

    def __init__(self, sessions: Sessions, jobs: InMemoryJobQueue, asset_id: str) -> None:
        super().__init__()
        self.sessions = sessions
        self.jobs = jobs
        self.asset_id = asset_id
        self.first_outcome: Outcome | None = None

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        await super().write_object(
            key, body, content_type=content_type, cache_control=cache_control
        )
        if self.first_outcome is None:
            view = InMemoryObjectStorage()
            view.objects = self.objects  # то же хранилище, но без этой подстановки
            view.modified = self.modified
            self.first_outcome = Outcome.SKIPPED  # защита от рекурсии: копия A работает по-честному
            self.first_outcome = await process(self.asset_id, self.sessions, view, self.jobs)


async def test_a_late_copy_of_the_task_leaves_the_files_of_the_ready_asset_alone(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await started(
        client, user, size_bytes=len(JPEG), purpose="avatar", content_type="image/jpeg"
    )
    asset_id = created["asset"]["id"]
    racing = FinishesFirst(sessionmaker, jobs, asset_id)
    put_object(racing, created, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'uploaded', uploaded_at = now(), size_bytes = :n WHERE id = :id",
        n=len(JPEG),
        id=uuid.UUID(asset_id),
    )

    late = await process(asset_id, sessionmaker, racing, jobs)

    assert racing.first_outcome is Outcome.READY  # копия A закончила раньше
    assert late is Outcome.DUPLICATE  # а копия B своего не получила и чужого не стёрла
    assert {key for key in racing.objects if key.startswith("public/")} == {
        f"public/avatars/{asset_id}/64.webp",
        f"public/avatars/{asset_id}/256.webp",
    }
    row = await row_of(admin_engine, asset_id)
    assert row["status"] == "ready"
    assert set(row["variants"]) == {"thumb", "medium"}
    assert await events_of(admin_engine) == ["AssetProcessed"]  # итог записан один раз


class Killed(BaseException):
    """Процесс убит посреди разбора (память) или задача отменена тайм-аутом: итога нет, исключения нет."""


def killer(calls: list[int]) -> Any:
    def render(*_args: object, **_kwargs: object) -> None:
        calls.append(1)
        raise Killed

    return render


async def attempts_of(engine: AsyncEngine, asset_id: str) -> int:
    return (await row_of(engine, asset_id))["processing_attempts"]


async def test_a_file_that_keeps_killing_the_decoder_is_rejected_on_the_fourth_try(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(process_module, "render_image", killer(calls))
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    asset_id = created["asset"]["id"]
    rejected = "media_rejected_total"
    before = sample(rejected, reason="processing_failed")

    for attempt in (1, 2, 3):  # три обрыва: сверка ставит задачу заново, и каждый раз воркер падает
        with pytest.raises(Killed):
            await process(asset_id, sessionmaker, storage, jobs)
        assert await attempts_of(admin_engine, asset_id) == attempt
        assert (await row_of(admin_engine, asset_id))["status"] == "processing"
    assert len(calls) == 3

    outcome = await process(asset_id, sessionmaker, storage, jobs)

    assert outcome is Outcome.REJECTED
    assert len(calls) == 3  # четвёртый раз файл даже не разбирали
    row = await row_of(admin_engine, asset_id)
    assert (row["status"], row["reject_reason"]) == ("rejected", "processing_failed")
    assert await events_of(admin_engine) == ["AssetUploaded", "AssetRejected"]
    assert len(jobs.named("delete_media_objects")) == 1  # объекты уберёт обычное удаление
    assert sample(rejected, reason="processing_failed") == before + 1


async def test_a_decode_that_ends_normally_forgets_the_earlier_breaks(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Воркер остановили при выкладке два раза посреди разбора: файл при этом ни в чём не виноват."""
    calls: list[int] = []
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    asset_id = created["asset"]["id"]
    with monkeypatch.context() as patched:
        patched.setattr(process_module, "render_image", killer(calls))
        for _ in range(2):
            with pytest.raises(Killed):
                await process(asset_id, sessionmaker, storage, jobs)
    assert await attempts_of(admin_engine, asset_id) == 2

    assert await process(asset_id, sessionmaker, storage, jobs) is Outcome.READY

    assert await attempts_of(admin_engine, asset_id) == 0


async def test_storage_failures_after_the_decode_are_not_held_against_the_file(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    """Сутки недоступного хранилища не должны превращать нормальный файл в «ядовитый»."""
    flaky = FlakyWrites(fail_after=0)
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG, content_type="image/jpeg")
    flaky.objects.update(storage.objects)
    asset_id = created["asset"]["id"]

    for _ in range(6):  # больше, чем MAX_DECODE_ATTEMPTS
        with pytest.raises(StorageUnavailableError):
            await process(asset_id, sessionmaker, flaky, jobs)
        assert await attempts_of(admin_engine, asset_id) == 0

    flaky.fail_after = 100
    assert await process(asset_id, sessionmaker, flaky, jobs) is Outcome.READY


async def test_files_rejected_before_the_decode_do_not_use_up_attempts(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    sessionmaker: Sessions,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, png_bomb(8000, 8000), content_type="image/png")

    assert await process(created["asset"]["id"], sessionmaker, storage, jobs) is Outcome.REJECTED

    assert await attempts_of(admin_engine, created["asset"]["id"]) == 0


def test_helper_constants_are_real_images() -> None:
    for payload in (JPEG, PNG, GIF, WEBP):
        assert Image.open(io.BytesIO(payload)).size == (48, 32)

"""Ссылки и карточки ресурса без БД (S6-03): аватар, фото, GIF, файл, ещё не готовое, заголовок скачивания."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from messunjerr.core.me import avatar_for
from messunjerr.media.domain.rules import (
    AVATARS_PREFIX,
    DEFAULT_FILENAME,
    UPLOADS_PREFIX,
    Kind,
    Purpose,
    attachment_disposition,
    object_key,
    object_keys,
    variant_key,
    variant_specs,
)
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.queries.presenter import AssetPresenter
from messunjerr.settings import Settings

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def settings_with(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr("postgresql+asyncpg://app:p@h/db"),
        "redis_url": SecretStr("redis://h/0"),
        "public_base_url": "https://site.test",
        "s3_endpoint_public": None,
        "s3_bucket": "media",
    }
    values.update(overrides)
    return Settings(**values)  # pyright: ignore[reportCallIssue]


def row_of(
    *,
    kind: Kind = Kind.IMAGE,
    purpose: Purpose = Purpose.POST,
    status: str = "ready",
    variants: dict[str, Any] | None = None,
    filename: str | None = "photo.jpg",
    content_type: str = "image/webp",
) -> AssetRow:
    asset_id = uuid.uuid4()
    return AssetRow(
        id=asset_id,
        owner_id=uuid.uuid4(),
        kind=kind,
        purpose=purpose,
        status=status,
        object_key=object_key(asset_id),
        original_filename=filename,
        content_type=content_type,
        declared_size=1000,
        size_bytes=700,
        width=640,
        height=480,
        variants=variants if variants is not None else {},
        created_at=NOW,
    )


def processed(row: AssetRow, *, with_original: bool = False) -> AssetRow:
    """Ресурс после обработки: варианты записаны (так их пишет `process_media`)."""
    purpose = Purpose(row.purpose)
    row.variants = {
        spec.name: {
            "key": variant_key(row.id, purpose, spec),
            "width": spec.size,
            "height": spec.size,
            "size": 10,
        }
        for spec in variant_specs(purpose)
    }
    if with_original:
        row.variants["original"] = {"key": row.object_key, "size": 500}
    return row


def presenter(storage: InMemoryObjectStorage, **overrides: Any) -> AssetPresenter:
    return AssetPresenter(storage, settings_with(**overrides))


async def test_a_photo_gets_two_signed_links_for_ten_minutes() -> None:
    storage = InMemoryObjectStorage()
    row = processed(row_of())

    links = await presenter(storage).links(row, now=NOW)

    assert links.expires_at == NOW + timedelta(minutes=10)
    assert links.urls.original is None
    for url, expected in ((links.urls.thumb, "thumb.webp"), (links.urls.medium, "medium.webp")):
        assert url is not None
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert parts.path == f"/media/{UPLOADS_PREFIX}{row.id}/{expected}"
        assert query["response-content-type"] == ["image/webp"]
        assert query["response-cache-control"] == ["private, max-age=300"]
    assert {request.expires_in for request in storage.presigned_gets} == {600}


async def test_the_link_lifetime_follows_the_setting() -> None:
    storage = InMemoryObjectStorage()

    links = await presenter(storage, download_url_ttl_seconds=120).links(
        processed(row_of()), now=NOW
    )

    assert links.expires_at == NOW + timedelta(seconds=120)
    assert {request.expires_in for request in storage.presigned_gets} == {120}


async def test_an_avatar_gets_public_addresses_without_signature_or_expiry() -> None:
    storage = InMemoryObjectStorage()
    row = processed(row_of(purpose=Purpose.AVATAR, content_type="image/webp"))

    links = await presenter(storage).links(row, now=NOW)

    base = f"https://site.test/media/{AVATARS_PREFIX}{row.id}"
    assert (links.urls.thumb, links.urls.medium, links.urls.original) == (
        f"{base}/64.webp",
        f"{base}/256.webp",
        None,
    )
    assert links.expires_at is None
    assert storage.presigned_gets == []
    # Относительные адреса профиля (`Avatar`) указывают на те же объекты.
    avatar = avatar_for(row.id)
    assert avatar is not None
    assert links.urls.thumb is not None
    assert links.urls.thumb.endswith(avatar.sm)
    assert links.urls.medium is not None
    assert links.urls.medium.endswith(avatar.md)


async def test_the_public_base_follows_the_storage_endpoint_when_it_is_set() -> None:
    row = processed(row_of(purpose=Purpose.GROUP_AVATAR))

    links = await presenter(
        InMemoryObjectStorage(), s3_endpoint_public="http://localhost:8333"
    ).links(row, now=NOW)

    assert links.urls.thumb == f"http://localhost:8333/media/{AVATARS_PREFIX}{row.id}/64.webp"


async def test_a_gif_also_links_its_original_as_a_gif() -> None:
    storage = InMemoryObjectStorage()
    row = processed(row_of(filename="fun.gif"), with_original=True)

    links = await presenter(storage).links(row, now=NOW)

    assert links.urls.original is not None
    assert f"/{UPLOADS_PREFIX}{row.id}/original" in links.urls.original
    original = next(r for r in storage.presigned_gets if r.key == row.object_key)
    assert original.content_type == "image/gif"
    assert links.expires_at == NOW + timedelta(minutes=10)


async def test_a_file_is_downloadable_only_as_an_attachment() -> None:
    storage = InMemoryObjectStorage()
    row = row_of(
        kind=Kind.FILE,
        purpose=Purpose.MESSAGE,
        filename='Отчёт "итог".pdf',
        content_type="application/pdf",
    )

    links = await presenter(storage).links(row, now=NOW)

    assert (links.urls.thumb, links.urls.medium) == (None, None)
    assert links.urls.original is not None
    request = storage.presigned_gets[0]
    assert request.key == row.object_key
    assert request.content_type == "application/octet-stream"
    assert request.content_disposition == attachment_disposition('Отчёт "итог".pdf')
    assert request.cache_control == "private, max-age=300"


@pytest.mark.parametrize("status", ["pending", "uploaded", "processing", "rejected", "deleted"])
async def test_nothing_is_linked_until_the_asset_is_ready(status: str) -> None:
    storage = InMemoryObjectStorage()

    links = await presenter(storage).links(processed(row_of(status=status)), now=NOW)

    assert (links.urls.thumb, links.urls.medium, links.urls.original, links.expires_at) == (
        None,
        None,
        None,
        None,
    )
    assert storage.presigned_gets == []


async def test_a_ready_image_of_the_s5_era_has_no_links_until_it_is_reprocessed() -> None:
    storage = InMemoryObjectStorage()

    links = await presenter(storage).links(row_of(variants={}), now=NOW)

    assert links.urls.thumb is None
    assert links.expires_at is None
    assert storage.presigned_gets == []


async def test_the_card_and_the_reference_carry_the_same_links() -> None:
    storage = InMemoryObjectStorage()
    row = processed(row_of())
    maker = presenter(storage)

    card = await maker.card(row, now=NOW)
    reference = await maker.ref(row, now=NOW)

    assert card.urls == reference.urls
    assert card.url_expires_at == reference.url_expires_at == NOW + timedelta(minutes=10)
    assert reference.model_dump(mode="json").keys() == {
        "id",
        "kind",
        "status",
        "content_type",
        "size_bytes",
        "filename",
        "width",
        "height",
        "urls",
        "url_expires_at",
    }
    assert (reference.width, reference.height, reference.size_bytes) == (640, 480, 700)
    assert reference.filename == "photo.jpg"


# ----------------------------------------------------------------------------- заголовок скачивания
@pytest.mark.parametrize(
    ("filename", "fallback"),
    [
        ("report.pdf", "report.pdf"),
        ('a"b.txt', "a_b.txt"),
        ("100%.txt", "100_.txt"),
        ("back\\slash.txt", "back_slash.txt"),
        ("Отчёт 2026.pdf", "_____ 2026.pdf"),
        ("名前.txt", "__.txt"),
    ],
)
def test_the_download_header_has_an_ascii_fallback_and_the_full_utf8_name(
    filename: str, fallback: str
) -> None:
    header = attachment_disposition(filename)

    assert header.startswith(f"attachment; filename=\"{fallback}\"; filename*=UTF-8''")
    encoded = header.split("filename*=UTF-8''", 1)[1]
    assert all(ch.isalnum() or ch in "-._~%" for ch in encoded)  # ничего, что ломает параметр
    assert "\r" not in header
    assert "\n" not in header


def test_an_empty_or_unprintable_name_falls_back_to_a_default() -> None:
    assert attachment_disposition("").startswith(f'attachment; filename="{DEFAULT_FILENAME}"')
    assert attachment_disposition("   ").startswith(f'attachment; filename="{DEFAULT_FILENAME}"')


def test_a_hostile_name_cannot_add_a_header_parameter_or_a_line() -> None:
    header = attachment_disposition('x.txt"; filename="evil.exe\r\nSet-Cookie: a=b')

    assert "\r" not in header
    assert "\n" not in header
    assert (
        header.count('"') == 2
    )  # только кавычки вокруг запасного имени: цитата не закрывается раньше
    assert header.endswith(
        "%0D%0ASet-Cookie%3A%20a%3Db"
    )  # полное имя передано процентным кодированием


# ----------------------------------------------------------------------------- ключи объектов
def test_all_possible_keys_of_an_asset_follow_its_purpose() -> None:
    asset_id = uuid.uuid4()

    assert object_keys(asset_id, Kind.IMAGE, Purpose.POST) == [
        f"uploads/{asset_id}/original",
        f"uploads/{asset_id}/thumb.webp",
        f"uploads/{asset_id}/medium.webp",
    ]
    assert object_keys(asset_id, Kind.IMAGE, Purpose.AVATAR) == [
        f"uploads/{asset_id}/original",
        f"public/avatars/{asset_id}/64.webp",
        f"public/avatars/{asset_id}/256.webp",
    ]
    assert object_keys(asset_id, Kind.IMAGE, Purpose.GROUP_AVATAR) == object_keys(
        asset_id, Kind.IMAGE, Purpose.AVATAR
    )
    assert object_keys(asset_id, Kind.FILE, Purpose.MESSAGE) == [f"uploads/{asset_id}/original"]

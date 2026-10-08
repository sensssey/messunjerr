"""Обработка файлов на стенде (S6): варианты WebP, публичные аватары через Caddy, ссылки на чтение, метрики.

Приёмка спринта на живой системе: настоящий API, Caddy, SeaweedFS, воркер очереди media (Pillow).
Фото с геометкой загружается так, как это делает браузер, а результат читается тем же путём:
публичный адрес аватара без подписи, закрытые варианты по подписанной ссылке.
"""

import io
import struct
import zlib
from typing import Any

import httpx
import pytest
from PIL import Image, ImageDraw

from .conftest import Account, Stand
from .test_uploads import MEDIA, eventually, put_file, start, status_of

PROFILE = "/api/v1/me/profile"
CACHE = "public, max-age=31536000, immutable"
PDF = b"%PDF-1.7\n" + b"stand-processing " * 50


def photo_with_secrets(size: tuple[int, int] = (640, 480)) -> bytes:
    """Фото с моделью устройства и геометкой в EXIF: после обработки их быть не должно."""
    image = Image.new("RGB", size, (200, 200, 200))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill=(255, 0, 0))
    exif = Image.Exif()
    exif[0x010F] = "SecretMaker"
    exif[0x0110] = "SecretModel"
    exif[0x8825] = {1: "N", 2: (55.0, 45.0, 21.0), 3: "E", 4: (37.0, 37.0, 4.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif, quality=90)
    return buffer.getvalue()


def png_bomb(width: int, height: int) -> bytes:
    """Однобитный PNG: заголовок называет огромный растр, а сжатые нули занимают килобайты."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(tag + payload))
        return struct.pack(">I", len(payload)) + tag + payload + crc

    row = b"\x00" * (1 + (width + 7) // 8)
    compressor = zlib.compressobj(9)
    stream = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", stream) + chunk(b"IEND", b"")
    )


async def uploaded_and_ready(
    client: httpx.AsyncClient, account: Account, body: bytes, **overrides: Any
) -> dict[str, Any]:
    """Заявка, `PUT` по ссылке через Caddy, завершение и ожидание воркера: карточка готового ресурса."""
    overrides.setdefault("size_bytes", len(body))
    created = await start(client, account, **overrides)
    asset_id = created["asset"]["id"]
    assert (await put_file(client, created, body)).status_code == 200
    done = await client.post(f"{MEDIA}/uploads/{asset_id}/complete", headers=account.headers)
    assert done.status_code == 202, done.text

    async def finished() -> bool:
        return (await status_of(client, account, asset_id))["status"] in ("ready", "rejected")

    await eventually(finished, what="обработка воркером media")
    return await status_of(client, account, asset_id)


async def delete_asset(client: httpx.AsyncClient, account: Account, asset_id: str) -> None:
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


async def gone(client: httpx.AsyncClient, url: str) -> bool:
    return (await client.get(url)).status_code == 404


# ----------------------------------------------------------------------------- аватар
async def test_an_avatar_is_public_immutable_and_free_of_metadata_and_a_replaced_one_disappears(
    client: httpx.AsyncClient, account: Account
) -> None:
    first = await uploaded_and_ready(
        client, account, photo_with_secrets(), purpose="avatar", filename="me.jpg"
    )
    assert first["status"] == "ready"
    first_id = first["id"]
    try:
        small_url, large_url = first["urls"]["thumb"], first["urls"]["medium"]
        assert small_url.endswith(f"/media/public/avatars/{first_id}/64.webp")
        assert first["url_expires_at"] is None  # адрес постоянный

        # Так её видит любой посетитель: без токена и без подписи, через Caddy.
        small = await client.get(small_url)
        large = await client.get(large_url)
        assert (small.status_code, large.status_code) == (200, 200), small.text
        assert small.headers["content-type"] == "image/webp"
        assert small.headers["cache-control"] == CACHE
        assert small.headers["x-content-type-options"] == "nosniff"
        assert (await client.head(small_url)).headers["cache-control"] == CACHE
        assert Image.open(io.BytesIO(small.content)).size == (64, 64)
        assert Image.open(io.BytesIO(large.content)).size == (256, 256)
        assert b"Secret" not in small.content + large.content  # ни модели, ни геометки

        # В профиле: относительные адреса, по ним тот же файл.
        patched = await client.patch(
            PROFILE, json={"avatar_asset_id": first_id}, headers=account.headers
        )
        assert patched.status_code == 200, patched.text
        avatar = patched.json()["avatar"]
        assert avatar == {
            "sm": f"/media/public/avatars/{first_id}/64.webp",
            "md": f"/media/public/avatars/{first_id}/256.webp",
        }
        via_profile = await client.get(avatar["sm"])
        assert via_profile.content == small.content

        # Замена удаляет прежний аватар вместе с файлами.
        second = await uploaded_and_ready(
            client, account, photo_with_secrets((300, 500)), purpose="avatar", filename="new.jpg"
        )
        assert second["status"] == "ready"
        try:
            replaced = await client.patch(
                PROFILE, json={"avatar_asset_id": second["id"]}, headers=account.headers
            )
            assert replaced.status_code == 200, replaced.text

            async def first_is_gone() -> bool:
                return await gone(client, small_url) and await gone(client, large_url)

            await eventually(first_is_gone, what="удаление файлов прежнего аватара")
            assert (await client.get(second["urls"]["thumb"])).status_code == 200
            assert (
                await client.get(f"{MEDIA}/{first_id}", headers=account.headers)
            ).status_code == 404
        finally:
            cleared = await client.patch(
                PROFILE, json={"avatar_asset_id": None}, headers=account.headers
            )
            assert cleared.status_code == 200
            await delete_asset(client, account, second["id"])
    finally:
        await delete_asset(client, account, first_id)


async def test_a_gif_and_a_foreign_file_cannot_become_an_avatar(
    client: httpx.AsyncClient, account: Account
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (0, 128, 0)).save(buffer, format="GIF")
    gif = buffer.getvalue()

    asset = await uploaded_and_ready(
        client, account, gif, purpose="avatar", content_type="image/png", filename="a.png"
    )

    assert (asset["status"], asset["reject_reason"]) == ("rejected", "unsupported_format")
    refused = await client.patch(
        PROFILE, json={"avatar_asset_id": asset["id"]}, headers=account.headers
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "asset_not_ready"
    await delete_asset(client, account, asset["id"])


# ----------------------------------------------------------------------------- закрытые файлы
async def test_a_photo_is_given_out_by_signed_links_and_a_file_as_an_attachment(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    secret_photo = photo_with_secrets()
    photo = await uploaded_and_ready(client, account, secret_photo, filename="trip.jpg")
    document = await uploaded_and_ready(
        client,
        account,
        PDF,
        purpose="message",
        filename="отчёт 2026.pdf",
        content_type="application/pdf",
    )
    try:
        assert (photo["status"], document["status"]) == ("ready", "ready")

        thumb = await client.get(photo["urls"]["thumb"])
        assert thumb.status_code == 200, thumb.text
        assert thumb.headers["content-type"] == "image/webp"
        assert thumb.headers["x-content-type-options"] == "nosniff"
        assert b"Secret" not in thumb.content
        # Подпись нельзя ни убрать, ни подделать, ни перенести на чужой файл.
        unsigned = photo["urls"]["thumb"].split("?", 1)[0]
        assert (await client.get(unsigned)).status_code == 403
        forged = photo["urls"]["thumb"].replace("X-Amz-Signature=", "X-Amz-Signature=00")
        assert (await client.get(forged)).status_code == 403

        file_link = document["urls"]["original"]
        download = await client.get(file_link)
        assert download.status_code == 200
        assert download.content == PDF
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["x-content-type-options"] == "nosniff"
        disposition = download.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert "filename*=UTF-8''" in disposition

        # Свежие ссылки по запросу: у владельца есть, у постороннего ресурса не существует.
        fresh = await client.get(f"{MEDIA}/{photo['id']}/urls", headers=account.headers)
        assert fresh.status_code == 200
        assert fresh.json()["urls"]["thumb"] is not None
        assert (await client.get(fresh.json()["urls"]["thumb"])).status_code == 200
        stranger = await client.get(f"{MEDIA}/{photo['id']}/urls", headers=other_account.headers)
        assert stranger.status_code == 404
    finally:
        await delete_asset(client, account, photo["id"])
        await delete_asset(client, account, document["id"])


# ----------------------------------------------------------------------------- ловушки
@pytest.mark.parametrize(
    ("body", "overrides", "reason"),
    [
        (
            png_bomb(8000, 8000),
            {"content_type": "image/png", "filename": "bomb.png"},
            "decompression_bomb",
        ),
        (
            png_bomb(5001, 5000),
            {"content_type": "image/png", "filename": "big.png"},
            "image_too_large",
        ),
        (b"<html><script>alert(1)</script></html>", {"content_type": "image/jpeg"}, "not_an_image"),
        (
            b"MZ"
            + b"\x90" * 58
            + (0x80).to_bytes(4, "little")
            + b"\x00" * 64
            + b"PE\x00\x00"
            + b"\x00" * 32,
            {"purpose": "message", "content_type": "application/octet-stream", "filename": "t.dat"},
            "forbidden_type",
        ),
    ],
)
async def test_traps_are_rejected_by_the_real_pipeline(
    client: httpx.AsyncClient,
    stand: Stand,
    account: Account,
    body: bytes,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    asset = await uploaded_and_ready(client, account, body, **overrides)

    assert (asset["status"], asset["reject_reason"]) == ("rejected", reason)
    assert asset["urls"] == {"thumb": None, "medium": None, "original": None}
    await delete_asset(client, account, asset["id"])


# ----------------------------------------------------------------------------- метрики
async def test_metrics_exist_inside_the_network_and_not_outside(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    assert (await client.get("/metrics")).status_code == 404  # снаружи через Caddy их нет
    assert (await client.get("/metrics/anything")).status_code == 404

    async with httpx.AsyncClient(timeout=15) as internal:
        api = await internal.get("http://api-a:8000/metrics")
        worker = await internal.get("http://worker-media:9102/metrics")

    assert api.status_code == 200
    assert api.headers["content-type"].startswith("text/plain; version=0.0.4")
    for name in (
        "http_requests_total",
        "db_pool_in_use",
        "arq_queue_depth",
        "http_request_duration_seconds",
    ):
        assert name in api.text, name
    assert worker.status_code == 200
    assert "media_processing_seconds" in worker.text
    assert "arq_jobs_total" in worker.text

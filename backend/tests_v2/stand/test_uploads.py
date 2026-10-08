"""Загрузка файлов на стенде (S5): настоящий API, Caddy, SeaweedFS, воркер очереди media.

Это приёмка спринта на живой системе: заявка в API, `PUT` по выданной ссылке через `https://messunjerr.localhost`,
завершение, обработка воркером, удаление объектов. Подписанные ссылки делает приложение (aiobotocore),
а не тест; тест только ходит по ним так же, как ходил бы браузер.
"""

import asyncio
import io
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from PIL import Image

from .conftest import RESET_HINT, Account, Stand
from .sigv4 import presign_url

MEDIA = "/api/v1/media"


def _photo() -> bytes:
    """Настоящий небольшой JPEG: обработка воркером открывает файл целиком."""
    image = Image.new("RGB", (96, 64), (90, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


JPEG = _photo()
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
PDF = b"%PDF-1.7\n" + b"stand " * 100


async def eventually(
    check: Callable[[], Awaitable[bool]], *, what: str, patience: float = 45.0
) -> None:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"не дождались: {what} за {patience:.0f} с")


def body_of(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "purpose": "post",
        "filename": "stand.jpg",
        "content_type": "image/jpeg",
        "size_bytes": len(JPEG),
    }
    body.update(overrides)
    return body


async def start(client: httpx.AsyncClient, account: Account, **overrides: Any) -> dict[str, Any]:
    response = await client.post(
        f"{MEDIA}/uploads", json=body_of(**overrides), headers=account.headers
    )
    if response.status_code == 429:
        pytest.fail(RESET_HINT)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def put_file(
    client: httpx.AsyncClient, created: dict[str, Any], body: bytes
) -> httpx.Response:
    """Загрузка так, как её делает браузер: `PUT` на выданную ссылку с выданными заголовками."""
    upload = created["upload"]
    return await client.put(upload["url"], content=body, headers=upload["headers"])


async def status_of(client: httpx.AsyncClient, account: Account, asset_id: str) -> dict[str, Any]:
    response = await client.get(f"{MEDIA}/{asset_id}", headers=account.headers)
    assert response.status_code == 200, response.text
    asset: dict[str, Any] = response.json()
    return asset


async def object_status(stand: Stand, asset_id: str, method: str = "GET") -> httpx.Response:
    """Объект в хранилище по внутреннему адресу и ключам приложения (мимо Caddy)."""
    url = presign_url(
        method,
        f"{stand.internal_s3_url}/media/uploads/{asset_id}/original",
        access_key=stand.s3_access_key,
        secret_key=stand.s3_secret_key,
    )
    async with httpx.AsyncClient() as internal:
        return await internal.request(method, url)


# ----------------------------------------------------------------------------- вся цепочка
async def test_a_file_goes_from_the_browser_to_ready_and_is_removed_with_its_object(
    client: httpx.AsyncClient, stand: Stand, account: Account
) -> None:
    created = await start(client, account)
    asset_id = created["asset"]["id"]
    assert created["upload"]["url"].startswith(
        f"{stand.base_url}/media/uploads/{asset_id}/original?"
    )
    assert created["upload"]["headers"] == {"Content-Type": "image/jpeg", "If-None-Match": "*"}

    uploaded = await put_file(client, created, JPEG)
    assert uploaded.status_code == 200, uploaded.text

    done = await client.post(f"{MEDIA}/uploads/{asset_id}/complete", headers=account.headers)
    assert done.status_code == 202, done.text
    assert done.json()["asset"]["status"] in ("uploaded", "processing", "ready")

    async def is_ready() -> bool:
        return (await status_of(client, account, asset_id))["status"] == "ready"

    await eventually(is_ready, what="обработка воркером media")
    asset = await status_of(client, account, asset_id)
    # Фото перекодировано в WebP: место считается по сохранённым вариантам, а не по загрузке.
    assert asset["content_type"] == "image/webp"
    assert (asset["width"], asset["height"]) == (96, 64)
    assert asset["size_bytes"] > 0
    for name in ("thumb", "medium"):
        link = asset["urls"][name]
        assert link.startswith(f"{stand.base_url}/media/uploads/{asset_id}/")
        shown = await client.get(link)  # так её откроет браузер: через Caddy, по подписи
        assert shown.status_code == 200, shown.text
        assert shown.headers["content-type"] == "image/webp"
    assert asset["urls"]["original"] is None
    assert (await object_status(stand, asset_id)).content == b""  # оригинал заменён пустым объектом

    quota = (await client.get(f"{MEDIA}/quota", headers=account.headers)).json()
    assert quota["used_bytes"] >= asset["size_bytes"]
    assert quota["assets_count"] >= 1

    assert (await object_status(stand, asset_id)).status_code == 200
    deleted = await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)
    assert deleted.status_code == 204

    async def object_is_gone() -> bool:
        return (await object_status(stand, asset_id)).status_code == 404

    await eventually(object_is_gone, what="удаление объекта задачей delete_media_objects")
    assert (await client.get(f"{MEDIA}/{asset_id}", headers=account.headers)).status_code == 404


async def test_an_svg_posing_as_a_png_is_rejected_and_its_object_is_removed(
    client: httpx.AsyncClient, stand: Stand, account: Account
) -> None:
    created = await start(
        client, account, filename="logo.png", content_type="image/png", size_bytes=len(SVG)
    )
    asset_id = created["asset"]["id"]
    assert (await put_file(client, created, SVG)).status_code == 200
    assert (
        await client.post(f"{MEDIA}/uploads/{asset_id}/complete", headers=account.headers)
    ).status_code == 202

    async def is_rejected() -> bool:
        return (await status_of(client, account, asset_id))["status"] == "rejected"

    await eventually(is_rejected, what="отказ обработки")
    asset = await status_of(client, account, asset_id)
    assert asset["reject_reason"] == "not_an_image"

    async def object_is_gone() -> bool:
        return (await object_status(stand, asset_id)).status_code == 404

    await eventually(object_is_gone, what="удаление объекта отклонённого файла")
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


async def test_documents_are_stored_as_octet_stream_whatever_the_client_declared(
    client: httpx.AsyncClient, stand: Stand, account: Account
) -> None:
    created = await start(
        client,
        account,
        purpose="message",
        filename="report.pdf",
        content_type="text/html",  # заявили разметку: хранилище её так не отдаст
        size_bytes=len(PDF),
    )
    asset_id = created["asset"]["id"]
    assert created["upload"]["headers"] == {
        "Content-Type": "application/octet-stream",
        "If-None-Match": "*",
    }
    assert (await put_file(client, created, PDF)).status_code == 200

    stored = await object_status(stand, asset_id, "HEAD")
    assert stored.status_code == 200
    assert stored.headers["content-type"] == "application/octet-stream"
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


# ----------------------------------------------------------------------------- подпись и границы
async def test_the_issued_link_pins_the_type_and_the_exact_size(
    client: httpx.AsyncClient, account: Account
) -> None:
    created = await start(client, account)
    asset_id = created["asset"]["id"]

    wrong_size = await client.put(
        created["upload"]["url"], content=JPEG + b"x", headers=created["upload"]["headers"]
    )
    wrong_type = await client.put(
        created["upload"]["url"],
        content=JPEG,
        headers={**created["upload"]["headers"], "Content-Type": "text/html"},
    )
    assert (wrong_size.status_code, wrong_type.status_code) == (403, 403)

    missing = await client.post(f"{MEDIA}/uploads/{asset_id}/complete", headers=account.headers)
    assert missing.status_code == 409
    assert missing.json()["code"] == "upload_missing"
    assert (await status_of(client, account, asset_id))["status"] == "pending"
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


async def test_the_link_creates_the_object_once_so_a_checked_file_cannot_be_swapped(
    client: httpx.AsyncClient, stand: Stand, account: Account
) -> None:
    created = await start(client, account)
    asset_id = created["asset"]["id"]
    assert (await put_file(client, created, JPEG)).status_code == 200

    swapped = await put_file(client, created, JPEG[:-1] + b"\x00")  # тот же размер, другие байты
    assert swapped.status_code == 412, swapped.text
    assert (await object_status(stand, asset_id)).content == JPEG

    # Условие входит в подпись: без заголовка запрос не проходит вовсе.
    bare = await client.put(
        created["upload"]["url"], content=JPEG, headers={"Content-Type": "image/jpeg"}
    )
    assert bare.status_code == 403
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


async def test_content_headers_in_a_put_do_not_become_metadata_of_the_object(
    client: httpx.AsyncClient, stand: Stand, account: Account
) -> None:
    """SeaweedFS хранит неподписанные заголовки о содержимом и отдаёт их при чтении: Caddy их срезает."""
    created = await start(client, account)
    asset_id = created["asset"]["id"]
    sent = {
        "Content-Encoding": "gzip",
        "Content-Disposition": "inline",
        "Cache-Control": "public, max-age=99999",
        "Expires": "Wed, 21 Oct 2037 07:28:00 GMT",
        "Content-Language": "xx",
    }

    uploaded = await client.put(
        created["upload"]["url"], content=JPEG, headers={**created["upload"]["headers"], **sent}
    )

    assert uploaded.status_code == 200, uploaded.text
    stored = await object_status(stand, asset_id, "HEAD")
    assert stored.status_code == 200
    assert {name: stored.headers.get(name) for name in sent} == dict.fromkeys(sent)
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)


async def test_requests_over_the_limits_get_the_catalog_codes(
    client: httpx.AsyncClient, account: Account
) -> None:
    too_big = await client.post(
        f"{MEDIA}/uploads",
        json=body_of(purpose="avatar", size_bytes=6 * 1024 * 1024),
        headers=account.headers,
    )
    assert too_big.status_code == 422
    assert too_big.json()["errors"][0]["code"] == "size_exceeds_limit"
    assert too_big.json()["errors"][0]["meta"]["max_bytes"] == 5 * 1024 * 1024

    forbidden = await client.post(
        f"{MEDIA}/uploads", json=body_of(filename="setup.exe"), headers=account.headers
    )
    assert forbidden.json()["errors"][0]["code"] == "extension_forbidden"


async def test_a_repeated_request_with_the_same_key_returns_the_same_asset(
    client: httpx.AsyncClient, account: Account
) -> None:
    headers = {**account.headers, "Idempotency-Key": str(uuid.uuid4())}
    first = await client.post(f"{MEDIA}/uploads", json=body_of(), headers=headers)
    again = await client.post(f"{MEDIA}/uploads", json=body_of(), headers=headers)

    assert first.status_code == again.status_code == 201
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json()["asset"]["id"] == first.json()["asset"]["id"]
    await client.delete(f"{MEDIA}/{first.json()['asset']['id']}", headers=account.headers)


async def test_the_api_and_the_storage_do_not_leak_across_accounts(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    created = await start(client, account)
    asset_id = created["asset"]["id"]

    # Чужой ресурс для другого человека не существует: те же ответы, что для случайного идентификатора.
    for method, path in (
        ("GET", f"{MEDIA}/{asset_id}"),
        ("POST", f"{MEDIA}/uploads/{asset_id}/complete"),
        ("DELETE", f"{MEDIA}/{asset_id}"),
    ):
        stranger = await client.request(method, path, headers=other_account.headers)
        assert stranger.status_code == 404, (method, stranger.text)
    assert (await status_of(client, account, asset_id))["status"] == "pending"  # владельцу виден

    unknown = uuid.uuid4()
    assert (await client.get(f"{MEDIA}/{unknown}", headers=account.headers)).status_code == 404
    assert (await client.delete(f"{MEDIA}/{unknown}", headers=account.headers)).status_code == 404
    assert (await client.get(f"{MEDIA}/{unknown}")).status_code == 401
    await client.delete(f"{MEDIA}/{asset_id}", headers=account.headers)

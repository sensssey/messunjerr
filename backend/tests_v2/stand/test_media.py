"""Спайк S4-05 (R3): presigned PUT и GET SeaweedFS за Caddy, публичный префикс `public/`.

Подпись SigV4 включает путь и заголовок Host, поэтому Caddy проксирует `/media/*` без переписывания.
Загрузку из настоящего браузера проверяет scripts/stand_browser_check.py (результат в плане, S4).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from .conftest import Stand, raw_request
from .sigv4 import presign_url

WEBP = {"Content-Type": "image/webp"}


def public_url(stand: Stand, key: str) -> str:
    return f"{stand.base_url}/media/{key}"


def sign(stand: Stand, method: str, url: str, **options: object) -> str:
    return presign_url(
        method,
        url,
        access_key=stand.s3_access_key,
        secret_key=stand.s3_secret_key,
        **options,  # type: ignore[arg-type]
    )


def private_key() -> str:
    return f"uploads/{uuid.uuid4().hex}/original.webp"


async def upload(client: httpx.AsyncClient, stand: Stand, key: str, body: bytes) -> None:
    """Загрузка так, как её делает браузер: presigned PUT с подписанными типом и размером."""
    url = sign(
        stand,
        "PUT",
        public_url(stand, key),
        signed_headers={"Content-Type": "image/webp", "Content-Length": str(len(body))},
    )
    response = await client.put(url, content=body, headers=WEBP)
    assert response.status_code == 200, response.text


async def worker_put_public(stand: Stand, key: str, body: bytes) -> None:
    """Воркер кладёт аватары в `public/` по внутреннему адресу, мимо Caddy."""
    headers = {"Content-Type": "image/webp", "Cache-Control": "public, max-age=31536000, immutable"}
    url = sign(stand, "PUT", f"{stand.internal_s3_url}/media/{key}", signed_headers=headers)
    async with httpx.AsyncClient() as internal:
        response = await internal.put(url, content=body, headers=headers)
    assert response.status_code == 200, response.text


# ----------------------------------------------------------------------------- приватные файлы
async def test_presigned_put_then_get_roundtrip_through_caddy(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key, body = private_key(), b"\x89WEBP" + b"\x00" * 4096
    await upload(client, stand, key, body)

    response = await client.get(sign(stand, "GET", public_url(stand, key)))
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"].strip('"') == hashlib.md5(body).hexdigest()  # noqa: S324


async def test_presigned_put_is_bound_to_content_type_and_size(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = private_key()
    url = sign(
        stand,
        "PUT",
        public_url(stand, key),
        signed_headers={"Content-Type": "image/webp", "Content-Length": "10"},
    )
    wrong_type = await client.put(url, content=b"x" * 10, headers={"Content-Type": "text/html"})
    assert wrong_type.status_code == 403
    too_big = await client.put(url, content=b"x" * 5000, headers=WEBP)
    assert too_big.status_code == 403
    exact = await client.put(url, content=b"x" * 10, headers=WEBP)
    assert exact.status_code == 200


async def test_private_object_is_not_readable_without_a_valid_signature(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = private_key()
    await upload(client, stand, key, b"secret")

    assert (await client.get(public_url(stand, key))).status_code == 403
    valid = sign(stand, "GET", public_url(stand, key))
    swapped = valid.replace(key, key.replace("original", "other"))
    assert (await client.get(swapped)).status_code == 403
    tampered = valid[:-4] + ("0000" if not valid.endswith("0000") else "1111")
    assert (await client.get(tampered)).status_code == 403


async def test_expired_presigned_url_is_refused(client: httpx.AsyncClient, stand: Stand) -> None:
    key = private_key()
    await upload(client, stand, key, b"data")
    long_ago = datetime.now(UTC) - timedelta(minutes=5)
    expired = sign(stand, "GET", public_url(stand, key), expires=60, now=long_ago)
    assert (await client.get(expired)).status_code == 403


async def test_signature_is_bound_to_the_host_that_was_signed(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = private_key()
    await upload(client, stand, key, b"data")
    url = sign(stand, "GET", public_url(stand, key))
    via_internal = url.replace(stand.base_url, stand.internal_s3_url)
    async with httpx.AsyncClient() as internal:
        assert (
            await internal.get(via_internal)
        ).status_code == 403  # Host другой: подпись не сошлась


async def test_presigned_delete_removes_the_object(client: httpx.AsyncClient, stand: Stand) -> None:
    key = private_key()
    await upload(client, stand, key, b"data")
    deleted = await client.delete(sign(stand, "DELETE", public_url(stand, key)))
    assert deleted.status_code in (200, 204)
    assert (await client.get(sign(stand, "GET", public_url(stand, key)))).status_code == 404


async def test_large_upload_roundtrip_through_caddy(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key, body = private_key(), bytes(range(256)) * 80_000  # около 20 МБ
    await upload(client, stand, key, body)
    try:
        response = await client.get(sign(stand, "GET", public_url(stand, key)))
        assert response.status_code == 200
        assert hashlib.sha256(response.content).digest() == hashlib.sha256(body).digest()
    finally:  # двадцать мегабайт не должны копиться в bucket с каждым прогоном
        await client.delete(sign(stand, "DELETE", public_url(stand, key)))


async def test_upload_of_the_largest_allowed_file_passes_the_edge(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    # file_max_bytes = 25 МиБ = 26 214 400 байт: Caddy считал MB десятичными и резал такой файл.
    key, body = private_key(), b"\x00" * 26_214_400
    await upload(client, stand, key, body)
    deleted = await client.delete(sign(stand, "DELETE", public_url(stand, key)))
    assert deleted.status_code in (200, 204)


async def test_upload_over_the_edge_limit_is_refused(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    body = b"\x00" * (27 * 1024 * 1024)  # предел Caddy для /media/* 26 МиБ
    key = private_key()
    url = sign(
        stand,
        "PUT",
        public_url(stand, key),
        signed_headers={"Content-Type": "image/webp", "Content-Length": str(len(body))},
    )
    try:
        response = await client.put(url, content=body, headers=WEBP)
    except httpx.TransportError:
        pass  # Caddy закрывает соединение, не дожидаясь конца тела: ответ 413 может не дойти
    else:
        assert response.status_code == 413
    # В любом случае объект не создан.
    stored = await client.get(sign(stand, "GET", public_url(stand, key)))
    assert stored.status_code == 404


# ----------------------------------------------------------------------------- публичный префикс
async def test_public_avatar_is_readable_anonymously_with_cache_headers(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = f"public/avatars/{uuid.uuid4()}/64.webp"
    await worker_put_public(stand, key, b"avatar")

    response = await client.get(public_url(stand, key))
    assert response.status_code == 200
    assert response.content == b"avatar"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert (await client.head(public_url(stand, key))).status_code == 200
    ranged = await client.get(public_url(stand, key), headers={"Range": "bytes=0-2"})
    assert ranged.status_code == 206
    assert ranged.content == b"ava"


async def test_internal_headers_of_seaweedfs_are_not_exposed(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = f"public/avatars/{uuid.uuid4()}/64.webp"
    await worker_put_public(stand, key, b"avatar")
    headers = (await client.get(public_url(stand, key))).headers
    for name in ("server", "x-amz-request-id", "seaweed-x-amz-owner", "seaweed-x-amz-etag"):
        assert name not in headers, name


async def test_nobody_writes_to_the_public_prefix_through_the_edge(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = f"public/avatars/{uuid.uuid4()}/64.webp"
    assert (await client.put(public_url(stand, key), content=b"evil")).status_code == 405
    signed_put = sign(stand, "PUT", public_url(stand, key), signed_headers=WEBP)
    assert (await client.put(signed_put, content=b"evil", headers=WEBP)).status_code == 405
    assert (await client.delete(public_url(stand, key))).status_code == 405
    assert (await client.get(public_url(stand, key))).status_code == 404  # объекта так и нет


async def test_public_prefix_listing_reveals_no_keys(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    # `GET /media/public/` SeaweedFS отвечает пустым `200 application/x-directory`, а не 403, как для
    # корня bucket: список ключей при этом не раскрывается ни в каком виде.
    identifier = uuid.uuid4()
    await worker_put_public(stand, f"public/avatars/{identifier}/64.webp", b"avatar")
    for target in (
        "/media/public/",
        "/media/public/?list-type=2",
        "/media/public/?prefix=avatars/",
        "/media/public/avatars/",
    ):
        response = await client.get(target)
        assert response.status_code in (200, 403, 404), target
        assert str(identifier) not in response.text, target


async def test_seaweedfs_itself_refuses_anonymous_writes_without_the_edge(stand: Stand) -> None:
    # Второй слой: права S3 (IAM), когда Caddy с его 405 в этой цепочке нет.
    url = f"{stand.internal_s3_url}/media/public/avatars/{uuid.uuid4()}/64.webp"
    async with httpx.AsyncClient() as internal:
        assert (await internal.put(url, content=b"evil")).status_code == 403
        assert (await internal.delete(url)).status_code == 403


async def test_anonymous_access_does_not_leak_outside_the_public_prefix(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    secret = private_key()
    await upload(client, stand, secret, b"top secret")
    await upload(client, stand, "public-secret/x.txt", b"top secret")
    await upload(client, stand, "exports/u1/e1.zip", b"top secret")

    for target in (
        f"/media/{secret}",
        "/media/public-secret/x.txt",
        "/media/exports/u1/e1.zip",
        "/media/?list-type=2",
        "/media/?list-type=2&prefix=public/",
        "/media/PUBLIC/avatars/x/64.webp",
    ):
        response = await client.get(target)
        assert response.status_code == 403, target
        assert b"top secret" not in response.content, target


@pytest.mark.parametrize(
    "target",
    [
        "/media/public/../uploads/x/original.webp",
        "/media/public/%2e%2e/uploads/x/original.webp",
        "/media/public/..%2fuploads/x/original.webp",
    ],
)
async def test_dot_segments_cannot_escape_the_public_prefix(
    client: httpx.AsyncClient, target: str
) -> None:
    response = await client.send(raw_request(client, target))
    assert response.status_code in (400, 403, 404), (target, response.status_code)


async def test_cors_is_limited_to_the_site_origin(client: httpx.AsyncClient, stand: Stand) -> None:
    key = f"public/avatars/{uuid.uuid4()}/64.webp"
    await worker_put_public(stand, key, b"avatar")
    # Положительный контроль: свой источник получает разрешение, иначе проверка чужого ничего не значит.
    own = await client.get(public_url(stand, key), headers={"Origin": stand.base_url})
    assert own.headers.get("access-control-allow-origin") == stand.base_url
    foreign = await client.get(public_url(stand, key), headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in foreign.headers

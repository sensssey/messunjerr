"""Граница стенда: TLS, заголовки безопасности, служебные адреса, статика, лимиты (S4-02)."""

import asyncio
import contextlib
import ssl
import uuid

import httpx
import pytest

from .conftest import Stand, raw_request


# ----------------------------------------------------------------------------- API через Caddy
async def test_meta_is_served_through_caddy_over_https(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["auth"]["methods"] == ["password"]
    assert body["version"]


async def test_the_request_id_is_created_by_caddy_or_kept_when_the_client_sent_one(
    client: httpx.AsyncClient,
) -> None:
    generated = (await client.get("/api/v1/meta")).headers["x-request-id"]
    assert uuid.UUID(generated)

    mine = f"stand-{uuid.uuid4().hex[:8]}"
    kept = await client.get("/api/v1/meta", headers={"X-Request-ID": mine})
    assert kept.headers["x-request-id"] == mine


async def test_problem_json_errors_pass_through_unchanged(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "token_missing"


async def test_jwks_is_published_next_to_the_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    assert response.json()["keys"][0]["kty"] == "OKP"


# ----------------------------------------------------------------------------- закрытые адреса
async def test_health_and_metrics_are_not_reachable_from_outside(client: httpx.AsyncClient) -> None:
    for path in (
        "/health/live",
        "/health/serving",
        "/health/ready",
        "/health",
        "/health/",
        "/metrics",
        "/metrics/x",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert "html" not in response.headers.get("content-type", ""), path  # не страница SPA


async def test_internal_paths_are_closed_for_every_method(client: httpx.AsyncClient) -> None:
    assert (await client.post("/health/ready")).status_code == 404
    assert (await client.request("DELETE", "/metrics")).status_code == 404


@pytest.mark.parametrize(
    "target",
    [
        "//health/live",
        "/health/../health/live",
        "/%68ealth/live",
        "/health%2flive",
        "/HEALTH/LIVE",
        "/api/../health/live",
        "/api/v1/../../health/ready",
        "/api/%2e%2e/health/live",
        "/%6detrics",
        "/api/v1/../../metrics",
        "/media/../health/live",
    ],
)
async def test_internal_paths_stay_closed_under_path_tricks(
    client: httpx.AsyncClient, target: str
) -> None:
    response = await client.send(raw_request(client, target))
    assert response.status_code == 404, target
    assert b'"status"' not in response.content, target  # ответа приложения нет


# ----------------------------------------------------------------------------- заголовки
async def test_security_headers_are_set_and_server_is_hidden(client: httpx.AsyncClient) -> None:
    headers = (await client.get("/api/v1/meta")).headers
    assert headers["strict-transport-security"].endswith("includeSubDomains")
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["permissions-policy"]
    assert "server" not in headers


async def test_swagger_ui_gets_its_own_csp_while_the_api_keeps_the_strict_one(
    client: httpx.AsyncClient,
) -> None:
    docs = await client.get("/api/v1/docs")
    assert docs.status_code == 200  # APP_ENV=stage: документация включена
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    meta = await client.get("/api/v1/meta")
    assert "cdn.jsdelivr.net" not in meta.headers["content-security-policy"]


# ----------------------------------------------------------------------------- TLS
async def test_http_redirects_to_https(stand: Stand) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as plain:
        response = await plain.get(f"http://{stand.host}/api/v1/meta")
    assert response.status_code == 308
    assert response.headers["location"] == f"https://{stand.host}/api/v1/meta"


async def test_certificate_comes_from_the_internal_ca_and_h2_is_negotiated(stand: Stand) -> None:
    context = ssl.create_default_context(cafile=stand.ca_file)
    context.set_alpn_protocols(["h2", "http/1.1"])
    _, writer = await asyncio.open_connection(
        stand.host, 443, ssl=context, server_hostname=stand.host
    )
    try:
        tls = writer.get_extra_info("ssl_object")
        assert tls.selected_alpn_protocol() == "h2"
        certificate = tls.getpeercert()
        assert certificate is not None
        assert ("DNS", stand.host) in certificate["subjectAltName"]
        issuer = dict(item[0] for item in certificate["issuer"])
        assert "Caddy Local Authority" in issuer["commonName"]
    finally:
        writer.close()
        with contextlib.suppress(ssl.SSLError):  # закрытие по TLS после h2-соединения шумит
            await writer.wait_closed()


# ----------------------------------------------------------------------------- статика и лимиты
async def test_placeholder_is_served_with_spa_fallback(client: httpx.AsyncClient) -> None:
    index = await client.get("/")
    assert index.status_code == 200
    assert "messunjerr" in index.text
    deep = await client.get("/profile/someone")
    assert deep.status_code == 200
    assert deep.text == index.text  # try_files {path} /index.html
    assert (await client.get("/style.css")).headers["content-type"].startswith("text/css")


@pytest.mark.parametrize("size", [1_200_000, 3_000_000])
async def test_api_body_over_one_mebibyte_is_refused_with_problem_json(
    client: httpx.AsyncClient, size: int
) -> None:
    # Предел 1 МиБ держит приложение: оно отвечает по заголовку Content-Length, не читая тело, поэтому
    # внешний предел Caddy (2 МиБ, `request_body`) срабатывает только на тех, кто тело читает: на /media/*.
    response = await client.post(
        "/api/v1/auth/login",
        content=b'{"x":"' + b"a" * size + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "payload_too_large"


async def test_responses_are_compressed_when_the_client_accepts_it(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/meta", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert response.json()["version"]

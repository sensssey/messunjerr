"""Служебные ручки на настоящих PostgreSQL и Redis: health, meta, документация."""

import re

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis

from messunjerr.core.redis import create_redis
from messunjerr.core.shutdown import ShutdownGate
from messunjerr.main import create_app
from messunjerr.settings import DEFAULT_REACTION_PALETTE, Settings

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


async def request(settings: Settings, path: str) -> httpx.Response:
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.get(path)


# ----------------------------------------------------------------------------- health
async def test_live_answers_without_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_serving_answers_while_the_process_takes_traffic(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/serving")
    assert response.status_code == 200
    assert response.json() == {"status": "serving"}


async def test_ready_when_everything_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "redis": "ok", "migrations": "head"},
        "degraded": {},
    }


async def test_not_ready_when_redis_is_down(test_settings: Settings) -> None:
    broken = test_settings.model_copy(update={"redis_url": SecretStr("redis://:x@127.0.0.1:1/0")})
    response = await request(broken, "/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] == "down"
    assert body["checks"]["postgres"] == "ok"


async def test_not_ready_when_postgres_is_down(test_settings: Settings) -> None:
    broken = test_settings.model_copy(
        update={"database_url": SecretStr("postgresql+asyncpg://app:x@127.0.0.1:1/none")}
    )
    response = await request(broken, "/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["postgres"] == "down"
    assert body["checks"]["migrations"] == "unknown"
    assert body["checks"]["redis"] == "ok"


async def test_not_ready_when_migrations_are_behind(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("messunjerr.main.expected_head", lambda: "9999")
    response = await request(test_settings, "/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "behind"


async def test_ready_when_the_database_is_ahead_of_the_code(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Старая реплика сразу после миграции новым релизом (или откат на старый код): БД на 0003,
    # а этому коду известны только 0001 и 0002. Выкладка без простоя требует остаться готовой.
    def known_to_the_old_code(revision: str, _ini_path: object = None) -> bool:
        return revision in {"0001", "0002"}

    monkeypatch.setattr("messunjerr.main.expected_head", lambda: "0002")
    monkeypatch.setattr("messunjerr.core.health.is_known_revision", known_to_the_old_code)
    response = await request(test_settings, "/health/ready")
    assert response.status_code == 200, response.text
    assert response.json()["checks"]["migrations"] == "ahead"


async def test_serving_and_ready_answer_503_while_the_process_drains_but_live_stays_ok(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.shutdown.begin()

    serving = await client.get("/health/serving")
    assert serving.status_code == 503
    assert serving.json() == {"status": "draining"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "unavailable",
        "checks": {"shutdown": "draining"},
        "degraded": {},
    }
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/api/v1/meta")).status_code == 200  # запросы пока принимаются


async def test_spike_streams_are_not_mounted_unless_enabled(
    client: httpx.AsyncClient, test_settings: Settings
) -> None:
    assert test_settings.spike_endpoints_enabled is False
    response = await client.get("/api/v1/_spike/sse")
    assert response.status_code == 404


async def test_spike_streams_are_mounted_when_enabled(test_settings: Settings) -> None:
    enabled = test_settings.model_copy(update={"spike_endpoints_enabled": True})
    app = create_app(enabled, shutdown_gate=ShutdownGate())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/api/v1/_spike/sse?count=1&interval=0.05")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: end" in response.text


@pytest.mark.parametrize("path", ["/health/live", "/health/serving"])
async def test_live_and_serving_do_not_depend_on_postgres_or_redis(
    test_settings: Settings, path: str
) -> None:
    # По /health/serving Caddy выводит реплику из балансировки: общий сбой PostgreSQL или Redis не
    # должен выводить из неё обе реплики разом (каждая отвечает ошибкой сама, а не 503 от Caddy).
    broken = test_settings.model_copy(
        update={
            "database_url": SecretStr("postgresql+asyncpg://app:x@127.0.0.1:1/none"),
            "redis_url": SecretStr("redis://:x@127.0.0.1:1/0"),
        }
    )
    response = await request(broken, path)
    assert response.status_code == 200


# ----------------------------------------------------------------------------- meta
async def test_meta_describes_limits_and_defaults(
    client: httpx.AsyncClient, test_settings: Settings
) -> None:
    response = await client.get("/api/v1/meta")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["version"] == "0.2.0"
    assert body["build"] == test_settings.app_build
    assert TIMESTAMP.match(body["server_time"])
    assert body["reactions"] == list(DEFAULT_REACTION_PALETTE)
    assert body["limits"]["post_body_max"] == 5000
    assert body["limits"]["message_body_max"] == 4000
    assert body["limits"]["group_members_max"] == test_settings.group_max_members
    assert body["limits"]["quota_bytes"] == 1024**3
    assert body["auth"] == {
        "methods": ["password"],
        "oauth_providers": [],
        "password_min_length": 10,
        "password_max_length": 128,
    }
    assert body["features"] == {"email_notifications": False, "data_export": False}


# ----------------------------------------------------------------------------- OpenAPI
async def test_openapi_is_available_outside_production(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert {"/health/live", "/health/serving", "/health/ready", "/api/v1/meta"} <= set(
        schema["paths"]
    )
    assert "Problem" in schema["components"]["schemas"]
    assert schema["info"]["title"] == "messunjerr API"


async def test_swagger_ui_is_available_outside_production(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()


async def test_documentation_is_off_in_production(test_settings: Settings) -> None:
    production = test_settings.model_copy(
        update={
            "app_env": "prod",
            # В prod хранилище обязательно (`check_runtime`); соединения оно не требует: клиент ленивый.
            "s3_endpoint_internal": "http://seaweedfs.invalid:8333",
            "s3_access_key": SecretStr("test-access"),
            "s3_secret_key": SecretStr("test-secret"),
        }
    )
    for path in ("/api/v1/docs", "/api/v1/openapi.json"):
        response = await request(production, path)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "not_found"


async def test_unknown_api_path_returns_problem_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["instance"] == "/api/v1/nope"


# ----------------------------------------------------------------------------- Redis
async def test_redis_client_round_trip(test_settings: Settings) -> None:
    client: Redis = create_redis(test_settings)
    try:
        assert await client.ping()  # pyright: ignore[reportUnknownMemberType]
        await client.set("probe", "значение", ex=30)
        assert await client.get("probe") == "значение"  # decode_responses=True
    finally:
        await client.aclose()

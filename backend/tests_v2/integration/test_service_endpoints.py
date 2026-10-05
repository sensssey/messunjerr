"""Служебные ручки на настоящих PostgreSQL и Redis: health, meta, документация."""

import re

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from redis.asyncio import Redis

from messunjerr.core.redis import create_redis
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


async def test_live_does_not_depend_on_postgres_or_redis(test_settings: Settings) -> None:
    broken = test_settings.model_copy(
        update={
            "database_url": SecretStr("postgresql+asyncpg://app:x@127.0.0.1:1/none"),
            "redis_url": SecretStr("redis://:x@127.0.0.1:1/0"),
        }
    )
    response = await request(broken, "/health/live")
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
    assert {"/health/live", "/health/ready", "/api/v1/meta"} <= set(schema["paths"])
    assert "Problem" in schema["components"]["schemas"]
    assert schema["info"]["title"] == "messunjerr API"


async def test_swagger_ui_is_available_outside_production(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()


async def test_documentation_is_off_in_production(test_settings: Settings) -> None:
    production = test_settings.model_copy(update={"app_env": "prod"})
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

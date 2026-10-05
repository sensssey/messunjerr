"""`GET /me` и проверка access-токена, `username-available`, JWKS, юридические документы, `/meta`."""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.services import IdentityServices

from .helpers import SignedInUser, bearer, execute, fetch_one, register, verified_user

ME = "/api/v1/me"


def tokens_of(app: FastAPI) -> TokenService:
    services: IdentityServices = app.state.identity
    return services.tokens


def problem(response: httpx.Response) -> dict[str, Any]:
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    return body


# ----------------------------------------------------------------------------- /me
async def test_me_returns_the_current_user(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.get(ME, headers=user.headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "id",
        "username",
        "email",
        "email_verified",
        "role",
        "status",
        "created_at",
    }
    assert body["id"] == user.user_id
    assert body["email"] == user.credentials["email"]
    assert body["username"] == user.credentials["username"]
    assert (body["email_verified"], body["role"], body["status"]) == (True, "user", "active")


async def test_me_without_a_token_is_401_with_a_bearer_challenge(client: httpx.AsyncClient) -> None:
    response = await client.get(ME)

    assert response.status_code == 401
    assert problem(response)["code"] == "token_missing"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "token abc"])
async def test_other_authorization_schemes_count_as_a_missing_token(
    client: httpx.AsyncClient, header: str
) -> None:
    response = await client.get(ME, headers={"Authorization": header})

    assert problem(response)["code"] == "token_missing"


async def test_garbage_token_is_invalid(client: httpx.AsyncClient) -> None:
    response = await client.get(ME, headers=bearer("definitely.not.ajwt"))

    assert response.status_code == 401
    assert problem(response)["code"] == "token_invalid"
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_expired_token_is_token_expired(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, app: FastAPI
) -> None:
    user = await verified_user(client, jobs)
    expired = tokens_of(app).issue(
        user_id=_uuid(user.user_id),
        session_id=_uuid(user.auth["session_id"]),
        role="user",
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    response = await client.get(ME, headers=bearer(expired.token))

    assert response.status_code == 401
    assert problem(response)["code"] == "token_expired"
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_token_signed_with_a_foreign_key_is_invalid(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, app: FastAPI
) -> None:
    user = await verified_user(client, jobs)
    own = tokens_of(app)
    forged = TokenService(
        private_key=Ed25519PrivateKey.generate(),
        key_id=own.key_id,  # даже с тем же kid подпись не сойдётся
        issuer="messunjerr",
        ttl_seconds=600,
    ).issue(user_id=_uuid(user.user_id), session_id=_uuid(user.auth["session_id"]), role="admin")

    response = await client.get(ME, headers=bearer(forged.token))

    assert problem(response)["code"] == "token_invalid"


async def test_token_with_a_restricted_scope_is_not_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, app: FastAPI
) -> None:
    user = await verified_user(client, jobs)
    limited = tokens_of(app).issue(
        user_id=_uuid(user.user_id),
        session_id=_uuid(user.auth["session_id"]),
        role="user",
        scope="consent",
    )

    response = await client.get(ME, headers=bearer(limited.token))

    assert problem(response)["code"] == "token_invalid"


async def test_revoked_session_is_rejected_through_the_denylist(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, redis_client: Redis
) -> None:
    user = await verified_user(client, jobs)
    assert (await client.get(ME, headers=user.headers)).status_code == 200

    await redis_client.set(f"sess:revoked:{user.auth['session_id']}", "1", ex=600)

    response = await client.get(ME, headers=user.headers)
    assert response.status_code == 401
    assert problem(response)["code"] == "session_revoked"


async def test_a_redis_outage_does_not_block_general_endpoints(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await verified_user(client, jobs)

    async def unreachable(*args: Any, **kwargs: Any) -> int:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(app.state.resources.redis, "exists", unreachable)

    assert (await client.get(ME, headers=user.headers)).status_code == 200


async def test_token_of_a_deleted_user_is_invalid(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "DELETE FROM identity.users")

    response = await client.get(ME, headers=user.headers)

    assert response.status_code == 401
    assert problem(response)["code"] == "token_invalid"


async def test_me_shows_changes_made_after_the_token_was_issued(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Статус читается из БД, а не из токена: токен несёт только идентификаторы и роль."""
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET status = 'deletion_pending'")

    response = await client.get(ME, headers=user.headers)

    assert response.json()["status"] == "deletion_pending"  # `GET /me` доступна и такому аккаунту


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


# ----------------------------------------------------------------------------- username-available
async def test_username_availability(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    taken = (await verified_user(client, jobs, username="ivan_taken")).credentials["username"]

    async def check(name: str) -> dict[str, Any]:
        response = await client.get("/api/v1/auth/username-available", params={"username": name})
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    assert await check("totally_free") == {"available": True, "reason": None}
    assert await check("Totally_Free") == {"available": True, "reason": None}
    assert await check(taken) == {"available": False, "reason": "taken"}
    assert await check(taken.upper()) == {"available": False, "reason": "taken"}
    assert await check("admin") == {"available": False, "reason": "reserved"}
    for bad in ("a", "bad name", "x" * 31, "иван", "a-b"):
        assert await check(bad) == {"available": False, "reason": "invalid"}


async def test_username_check_requires_a_value(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/username-available", params={"username": ""})

    assert response.status_code == 422
    assert (await client.get("/api/v1/auth/username-available")).status_code == 422


async def test_username_of_a_pending_account_counts_as_taken(client: httpx.AsyncClient) -> None:
    _, body = await register(client)

    response = await client.get(
        "/api/v1/auth/username-available", params={"username": body["username"]}
    )

    assert response.json() == {"available": False, "reason": "taken"}


# ----------------------------------------------------------------------------- JWKS
async def test_jwks_publishes_the_key_that_signs_access_tokens(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user: SignedInUser = await verified_user(client, jobs)

    response = await client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    (jwk,) = response.json()["keys"]
    assert (jwk["kty"], jwk["crv"], jwk["use"], jwk["alg"]) == ("OKP", "Ed25519", "sig", "EdDSA")
    assert "d" not in jwk
    assert len(base64.urlsafe_b64decode(jwk["x"] + "==")) == 32
    token = user.auth["access_token"]
    assert jwt.get_unverified_header(token)["kid"] == jwk["kid"]
    decoded = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["EdDSA"], audience="messunjerr-api")
    assert decoded["sub"] == user.user_id
    assert decoded["sid"] == user.auth["session_id"]
    assert decoded["iss"] == "messunjerr"


# ----------------------------------------------------------------------------- юридический минимум
async def test_legal_documents(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/legal/documents")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["min_age"] == 18
    assert set(body["operator"]) == {"name", "address", "contact_email"}
    (document,) = body["items"]
    assert document["slug"] == "terms"
    assert document["version"] == "2026-10-01"
    assert "персональных данных" in document["title"]
    assert document["url"] == "/legal/terms"


async def test_meta_announces_the_legal_parameters(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/meta")).json()

    assert body["legal"] == {
        "min_age": 18,
        "documents": [{"slug": "terms", "version": "2026-10-01"}],
    }


async def test_the_accepted_terms_version_matches_what_the_api_publishes(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    published = (await client.get("/api/v1/legal/documents")).json()["items"][0]["version"]
    await register(client)

    stored = (await fetch_one(admin_engine, "SELECT terms_version FROM identity.users"))[
        "terms_version"
    ]
    assert stored == published


# ----------------------------------------------------------------------------- OpenAPI
async def test_openapi_describes_the_identity_endpoints(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    paths = schema["paths"]
    assert "post" in paths["/api/v1/auth/register"]
    assert "post" in paths["/api/v1/auth/verify-email"]
    assert "post" in paths["/api/v1/auth/resend-verification"]
    assert "post" in paths["/api/v1/auth/login"]
    assert "get" in paths["/api/v1/auth/username-available"]
    assert "get" in paths["/api/v1/me"]
    assert "get" in paths["/api/v1/legal/documents"]
    assert "get" in paths["/.well-known/jwks.json"]
    assert schema["components"]["securitySchemes"]  # кнопка Authorize в Swagger UI
    register = paths["/api/v1/auth/register"]["post"]
    assert {"201", "409", "422", "503"} <= set(register["responses"])
    assert "accept_terms" in schema["components"]["schemas"]["RegisterRequest"]["properties"]

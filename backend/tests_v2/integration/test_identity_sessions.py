"""Выход и сессии: `/auth/logout`, `/auth/logout-all`, `GET /auth/sessions`, `DELETE /auth/sessions/{id}`."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.services import IdentityServices

from .helpers import (
    CSRF,
    ME,
    PASSWORD,
    SignedInUser,
    bearer,
    cookie_header,
    do_refresh,
    execute,
    fetch_all,
    fetch_one,
    login_again,
    refresh_headers,
    verified_user,
)

LOGOUT = "/api/v1/auth/logout"
LOGOUT_ALL = "/api/v1/auth/logout-all"
SESSIONS = "/api/v1/auth/sessions"


def is_cleared(response: httpx.Response) -> bool:
    """Ответ стирает cookie с refresh-токеном (`Max-Age=0`)."""
    header = response.headers.get("set-cookie", "").lower()
    return "__secure-mj_refresh=" in header and "max-age=0" in header


async def revoked_reason(engine: AsyncEngine, session_id: str) -> str | None:
    row = await fetch_one(
        engine, "SELECT revoked_reason FROM identity.sessions WHERE id = :id", id=session_id
    )
    return row["revoked_reason"]


def break_redis(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    """Redis «недоступен» для проверки отозванных сессий."""
    services: IdentityServices = app.state.identity

    async def unreachable(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(services.denylist, "access_state", unreachable)


# ----------------------------------------------------------------------------- logout
async def test_logout_closes_the_session_everywhere(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    user = await verified_user(client, jobs)

    response = await client.post(LOGOUT, headers=refresh_headers(user.refresh_token))

    assert response.status_code == 204
    assert response.content == b""
    assert is_cleared(response)
    assert await revoked_reason(admin_engine, user.session_id) == "logout"
    assert await redis_client.exists(f"sess:revoked:{user.session_id}") == 1
    stale = await client.get(ME, headers=user.headers)  # access-токен жив по сроку, но сессии нет
    assert (stale.status_code, stale.json()["code"]) == (401, "session_revoked")
    assert (await do_refresh(client, user.refresh_token)).json()["code"] == "refresh_invalid"
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'logout'"
    )
    assert audit["target_id"] == user.session_id


@pytest.mark.parametrize("cookie", [None, "garbage-token", "x" * 43])
async def test_logout_is_idempotent_and_always_clears_the_cookie(
    client: httpx.AsyncClient, cookie: str | None
) -> None:
    headers = {**CSRF, **(cookie_header(cookie) if cookie else {})}

    first = await client.post(LOGOUT, headers=headers)
    second = await client.post(LOGOUT, headers=headers)

    assert (first.status_code, second.status_code) == (204, 204)
    assert is_cleared(first)


async def test_logout_twice_with_the_same_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    await client.post(LOGOUT, headers=refresh_headers(user.refresh_token))
    again = await client.post(LOGOUT, headers=refresh_headers(user.refresh_token))

    assert again.status_code == 204
    logs = await fetch_all(
        admin_engine, "SELECT id FROM platform.audit_log WHERE action = 'logout'"
    )
    assert len(logs) == 1  # второй выход ничего не менял и не пишется


async def test_logout_with_a_token_that_was_just_rotated(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Вкладка со старым токеном выходит сразу после ротации в другой вкладке: сессия закрывается."""
    user = await verified_user(client, jobs)
    await do_refresh(client, user.refresh_token)

    response = await client.post(LOGOUT, headers=refresh_headers(user.refresh_token))

    assert response.status_code == 204
    assert await revoked_reason(admin_engine, user.session_id) == "logout"


async def test_logout_leaves_other_devices_alone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    await client.post(LOGOUT, headers=refresh_headers(laptop.refresh_token))

    assert (await client.get(ME, headers=phone.headers)).status_code == 200
    assert (await do_refresh(client, phone.refresh_token)).status_code == 200


async def test_logout_requires_the_csrf_headers(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    response = await client.post(LOGOUT, headers=cookie_header(user.refresh_token))

    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")
    assert await revoked_reason(admin_engine, user.session_id) is None


# ----------------------------------------------------------------------------- logout-all
async def test_logout_all_with_a_wrong_password_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    response = await client.post(
        LOGOUT_ALL, json={"password": "not my password"}, headers=laptop.headers
    )

    assert (response.status_code, response.json()["code"]) == (403, "reauth_failed")
    assert "set-cookie" not in response.headers
    assert (await client.get(ME, headers=phone.headers)).status_code == 200
    assert await revoked_reason(admin_engine, laptop.session_id) is None
    failure = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'reauth.failure'"
    )
    assert str(failure["actor_id"]) == laptop.user_id


async def test_logout_all_closes_every_session(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    response = await client.post(LOGOUT_ALL, json={"password": PASSWORD}, headers=laptop.headers)

    assert response.status_code == 204
    assert is_cleared(response)
    for session in (laptop, phone):
        assert await revoked_reason(admin_engine, session.session_id) == "logout_all"
        assert await redis_client.exists(f"sess:revoked:{session.session_id}") == 1
        denied = await client.get(ME, headers=session.headers)
        assert (denied.status_code, denied.json()["code"]) == (401, "session_revoked")
        assert (await do_refresh(client, session.refresh_token)).json()["code"] == "refresh_invalid"
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'logout_all'"
    )
    assert audit["data"] == {"revoked": 2, "keep_current": False}


async def test_logout_all_can_keep_the_current_session(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    response = await client.post(
        LOGOUT_ALL, json={"password": PASSWORD, "keep_current": True}, headers=laptop.headers
    )

    assert response.status_code == 204
    assert "set-cookie" not in response.headers  # cookie текущей сессии остаётся
    assert (await client.get(ME, headers=laptop.headers)).status_code == 200
    assert (await client.get(ME, headers=phone.headers)).status_code == 401
    assert await revoked_reason(admin_engine, laptop.session_id) is None


async def test_logout_all_does_not_touch_other_users(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    mine = await verified_user(client, jobs)
    other = await verified_user(client, jobs)

    await client.post(LOGOUT_ALL, json={"password": PASSWORD}, headers=mine.headers)

    assert (await client.get(ME, headers=other.headers)).status_code == 200


@pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": "x", "keep_current": "yes"}])
async def test_logout_all_validates_the_body(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, body: dict[str, Any]
) -> None:
    user = await verified_user(client, jobs)

    response = await client.post(LOGOUT_ALL, json=body, headers=user.headers)

    assert response.status_code == 422


async def test_logout_all_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.post(LOGOUT_ALL, json={"password": PASSWORD})

    assert (response.status_code, response.json()["code"]) == (401, "token_missing")


# ----------------------------------------------------------------------------- список сессий
async def test_sessions_are_listed_with_the_current_one_marked(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(
        client,
        laptop,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"
        },
    )

    response = await client.get(SESSIONS, headers=laptop.headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert {item["id"] for item in items} == {laptop.session_id, phone.session_id}
    by_id = {item["id"]: item for item in items}
    assert by_id[laptop.session_id]["current"] is True
    assert by_id[phone.session_id]["current"] is False
    assert set(by_id[phone.session_id]) == {
        "id",
        "device_label",
        "user_agent",
        "ip_masked",
        "created_at",
        "last_seen_at",
        "current",
    }
    assert by_id[phone.session_id]["device_label"] == "Firefox на Windows"  # из User-Agent
    assert by_id[phone.session_id]["ip_masked"] == "127.0.0.x"
    assert by_id[phone.session_id]["created_at"].endswith("Z")


async def test_a_label_given_at_login_wins_over_the_user_agent(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await client.post(
        "/api/v1/auth/login",
        json={
            "login": user.credentials["email"],
            "password": PASSWORD,
            "device_label": "Мой ноутбук",
        },
        headers={"User-Agent": "curl/8.5.0"},
    )

    items = (await client.get(SESSIONS, headers=user.headers)).json()["items"]

    assert "Мой ноутбук" in {item["device_label"] for item in items}


async def test_only_live_sessions_of_the_user_are_listed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    me = await verified_user(client, jobs)
    revoked = await login_again(client, me)
    expired = await login_again(client, me)
    await verified_user(client, jobs)  # чужие сессии в список не попадают
    await execute(
        admin_engine,
        "UPDATE identity.sessions SET revoked_at = now() WHERE id = :id",
        id=revoked.session_id,
    )
    await execute(
        admin_engine,
        "UPDATE identity.sessions SET expires_at = now() - interval '1 second' WHERE id = :id",
        id=expired.session_id,
    )

    items = (await client.get(SESSIONS, headers=me.headers)).json()["items"]

    assert [item["id"] for item in items] == [me.session_id]


async def test_sessions_require_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(SESSIONS)).status_code == 401


# ----------------------------------------------------------------------------- закрыть одну сессию
async def test_a_session_can_be_closed_from_another_device(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    response = await client.delete(f"{SESSIONS}/{phone.session_id}", headers=laptop.headers)

    assert response.status_code == 204
    assert "set-cookie" not in response.headers  # закрыта не та сессия, с которой пришёл запрос
    assert await revoked_reason(admin_engine, phone.session_id) == "logout"
    assert await redis_client.exists(f"sess:revoked:{phone.session_id}") == 1
    assert (await client.get(ME, headers=phone.headers)).status_code == 401
    assert (await client.get(ME, headers=laptop.headers)).status_code == 200
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'session.revoked'"
    )
    assert audit["target_id"] == phone.session_id


async def test_closing_the_current_session_behaves_like_logout(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.delete(f"{SESSIONS}/{user.session_id}", headers=user.headers)

    assert response.status_code == 204
    assert is_cleared(response)
    assert (await client.get(ME, headers=user.headers)).status_code == 401


async def test_foreign_unknown_and_closed_sessions_are_all_404(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    mine = await verified_user(client, jobs)
    foreign = await verified_user(client, jobs)
    closed = await login_again(client, mine)
    await client.delete(f"{SESSIONS}/{closed.session_id}", headers=mine.headers)

    for session_id in (
        foreign.session_id,
        "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
        closed.session_id,
    ):
        response = await client.delete(f"{SESSIONS}/{session_id}", headers=mine.headers)
        assert (response.status_code, response.json()["code"]) == (404, "not_found")

    assert await revoked_reason(admin_engine, foreign.session_id) is None  # чужая сессия цела


async def test_a_malformed_session_id_is_a_validation_error(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.delete(f"{SESSIONS}/not-a-uuid", headers=user.headers)

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/path/session_id"


# ----------------------------------------------------------------------------- без Redis
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", SESSIONS, None),
        ("DELETE", f"{SESSIONS}/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80", None),
        ("POST", LOGOUT_ALL, {"password": PASSWORD}),
    ],
)
async def test_sensitive_endpoints_answer_503_when_redis_is_down(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    user: SignedInUser = await verified_user(client, jobs)
    break_redis(monkeypatch, app)

    response = await client.request(method, path, json=body, headers=user.headers)

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    assert await revoked_reason(admin_engine, user.session_id) is None  # ничего не изменилось


async def test_general_endpoints_keep_working_when_redis_is_down(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await verified_user(client, jobs)
    break_redis(monkeypatch, app)

    assert (await client.get(ME, headers=user.headers)).status_code == 200
    assert (await do_refresh(client, user.refresh_token)).status_code == 200


# ----------------------------------------------------------------------------- вход после выхода
async def test_logging_in_again_after_logout_all_works(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await client.post(LOGOUT_ALL, json={"password": PASSWORD}, headers=user.headers)

    fresh = await login_again(client, user)

    assert (await client.get(ME, headers=bearer(fresh.auth["access_token"]))).status_code == 200
    assert fresh.session_id != user.session_id

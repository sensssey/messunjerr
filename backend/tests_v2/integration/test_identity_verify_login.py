"""Подтверждение почты, повторная отправка и вход: `/auth/verify-email`, `/auth/resend-verification`, `/auth/login`."""

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.services import IdentityServices

from .helpers import (
    PASSWORD,
    REFRESH_COOKIE,
    execute,
    fetch_all,
    fetch_one,
    register,
    verification_token,
    verified_user,
)

VERIFY = "/api/v1/auth/verify-email"
RESEND = "/api/v1/auth/resend-verification"
LOGIN = "/api/v1/auth/login"
CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def identity_of(app: FastAPI) -> IdentityServices:
    services: IdentityServices = app.state.identity
    return services


def cookie_attributes(response: httpx.Response) -> dict[str, str]:
    """Атрибуты Set-Cookie: имя куки и всё, что после `;`, в нижнем регистре."""
    header = response.headers["set-cookie"]
    name, _, rest = header.partition("=")
    attributes: dict[str, str] = {"name": name}
    for part in rest.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        attributes[key.lower()] = value
    return attributes


# ----------------------------------------------------------------------------- подтверждение почты
async def test_verification_activates_the_account_and_signs_in(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, app: FastAPI
) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])

    response = await client.post(VERIFY, json={"token": token})

    assert response.status_code == 200
    auth = response.json()
    assert set(auth) == {"access_token", "token_type", "expires_in", "session_id", "user"}
    assert auth["token_type"] == "Bearer"
    assert auth["expires_in"] == 600
    user = auth["user"]
    assert user["email"] == body["email"]
    assert user["username"] == body["username"]
    assert (user["email_verified"], user["status"], user["role"]) == (True, "active", "user")
    assert CREATED_AT.match(user["created_at"])
    assert response.headers["cache-control"] == "no-store"

    row = await fetch_one(admin_engine, "SELECT * FROM identity.users")
    assert row["status"] == "active"
    assert row["email_verified_at"] is not None
    assert row["last_login_at"] is not None
    consumed = await fetch_one(admin_engine, "SELECT consumed_at FROM identity.email_tokens")
    assert consumed["consumed_at"] is not None

    claims = identity_of(app).tokens.verify(auth["access_token"])
    assert str(claims.user_id) == user["id"]
    assert str(claims.session_id) == auth["session_id"]
    assert claims.role == "user"


async def test_refresh_token_comes_only_in_a_strict_http_only_cookie(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    attributes = cookie_attributes(user.response)
    assert attributes["name"] == REFRESH_COOKIE
    assert attributes["path"] == "/api/v1/auth"
    assert attributes["max-age"] == str(30 * 24 * 3600)
    assert attributes["samesite"].lower() == "strict"
    assert "httponly" in attributes
    assert "secure" in attributes
    assert user.refresh_token not in user.response.text  # в теле токена нет

    session = await fetch_one(admin_engine, "SELECT * FROM identity.sessions")
    assert str(session["id"]) == user.auth["session_id"]
    assert str(session["user_id"]) == user.user_id
    assert bytes(session["refresh_hash"]) == hashlib.sha256(user.refresh_token.encode()).digest()
    assert session["prev_refresh_hash"] is None
    assert session["revoked_at"] is None
    assert str(session["ip"]) == "127.0.0.1"
    now = datetime.now(UTC)
    assert timedelta(days=29, hours=23) < session["expires_at"] - now <= timedelta(days=30)
    assert timedelta(days=89, hours=23) < session["absolute_expires_at"] - now <= timedelta(days=90)


async def test_verification_events_are_recorded_in_order(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    events = await fetch_all(admin_engine, "SELECT * FROM platform.outbox ORDER BY id")
    assert [e["event_type"] for e in events] == ["UserRegistered", "EmailVerified"]
    assert all(e["payload"] == {"user_id": user.user_id} for e in events)
    assert all(e["topic"] == "mj.identity.user.v1" for e in events)


async def test_token_works_only_once(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])

    first = await client.post(VERIFY, json={"token": token})
    second = await client.post(VERIFY, json={"token": token})

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["code"] == "token_invalid_or_expired"
    assert "set-cookie" not in second.headers


async def test_two_simultaneous_requests_with_one_token_give_one_session(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])

    responses = await asyncio.gather(
        *(client.post(VERIFY, json={"token": token}) for _ in range(4))
    )

    assert sorted(r.status_code for r in responses) == [200, 400, 400, 400]
    assert len(await fetch_all(admin_engine, "SELECT id FROM identity.sessions")) == 1
    events = await fetch_all(admin_engine, "SELECT event_type FROM platform.outbox")
    assert [e["event_type"] for e in events].count("EmailVerified") == 1


async def test_expired_token_is_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])
    await execute(
        admin_engine, "UPDATE identity.email_tokens SET expires_at = now() - interval '1 second'"
    )

    response = await client.post(VERIFY, json={"token": token})

    assert response.status_code == 400
    assert response.json()["code"] == "token_invalid_or_expired"
    assert (await fetch_one(admin_engine, "SELECT status FROM identity.users"))[
        "status"
    ] == "pending"


@pytest.mark.parametrize("token", ["x" * 43, "garbage", "../../etc/passwd", "' OR 1=1 --"])
async def test_unknown_token_is_rejected(client: httpx.AsyncClient, token: str) -> None:
    response = await client.post(VERIFY, json={"token": token})

    assert response.status_code == 400
    assert response.json()["code"] == "token_invalid_or_expired"


async def test_token_of_another_purpose_is_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])
    await execute(admin_engine, "UPDATE identity.email_tokens SET purpose = 'reset_password'")

    response = await client.post(VERIFY, json={"token": token})

    assert response.status_code == 400


async def test_empty_and_oversized_tokens_fail_validation(client: httpx.AsyncClient) -> None:
    empty = await client.post(VERIFY, json={"token": ""})
    huge = await client.post(VERIFY, json={"token": "t" * 300})

    assert (empty.status_code, huge.status_code) == (422, 422)


async def test_a_banned_account_does_not_get_a_session_and_keeps_its_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)
    token = verification_token(jobs, body["email"])
    await execute(admin_engine, "UPDATE identity.users SET status = 'banned'")

    response = await client.post(VERIFY, json={"token": token})

    assert response.status_code == 403
    assert response.json()["code"] == "account_banned"
    assert "set-cookie" not in response.headers
    assert (await fetch_one(admin_engine, "SELECT consumed_at FROM identity.email_tokens"))[
        "consumed_at"
    ] is None
    assert await fetch_all(admin_engine, "SELECT id FROM identity.sessions") == []


# ----------------------------------------------------------------------------- повторная отправка
async def test_resend_issues_a_new_token_and_retires_the_old_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    _, body = await register(client)
    old = verification_token(jobs, body["email"])
    jobs.clear()

    response = await client.post(RESEND, json={"email": body["email"]})

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    new = verification_token(jobs, body["email"])
    assert new != old
    assert (await client.post(VERIFY, json={"token": old})).status_code == 400
    assert (await client.post(VERIFY, json={"token": new})).status_code == 200


async def test_resend_answers_identically_for_pending_unknown_and_active_addresses(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    _, pending = await register(client)
    active = await verified_user(client, jobs)
    jobs.clear()

    answers = [
        await client.post(RESEND, json={"email": address})
        for address in (
            pending["email"],
            active.credentials["email"],
            "nobody-at-all@example.com",
        )
    ]

    assert {r.status_code for r in answers} == {202}
    assert {r.text for r in answers} == {'{"status":"accepted"}'}
    assert {r.headers["cache-control"] for r in answers} == {"no-store"}
    # Письмо уходит только неподтверждённому аккаунту; по ответу это не видно.
    assert [job.kwargs["to"] for job in jobs.named("send_email")] == [pending["email"]]


async def test_resend_for_an_invalid_address_is_a_validation_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(RESEND, json={"email": "not-an-address"})

    assert response.status_code == 422


async def test_resend_does_not_lock_the_owner_out_when_the_queue_is_down(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """Сбой очереди откатывает выдачу нового токена: прежний остаётся рабочим."""
    _, body = await register(client)
    old = verification_token(jobs, body["email"])
    jobs.fail_with = ConnectionError("redis is down")

    response = await client.post(RESEND, json={"email": body["email"]})

    assert response.status_code == 202  # ответ не зависит ни от адреса, ни от сбоя
    jobs.fail_with = None
    assert (await client.post(VERIFY, json={"token": old})).status_code == 200


# ----------------------------------------------------------------------------- вход
async def test_login_by_email_or_username_in_any_case(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    email, username = user.credentials["email"], user.credentials["username"]

    for login in (email, email.upper(), username, username.upper()):
        response = await client.post(
            LOGIN,
            json={"login": login, "password": PASSWORD, "device_label": "Firefox на Windows"},
            headers={"User-Agent": "Mozilla/5.0 (test)"},
        )
        assert response.status_code == 200, login
        auth = response.json()
        assert auth["user"]["id"] == user.user_id
        assert auth["token_type"] == "Bearer"
        assert attributes_name(response) == REFRESH_COOKIE

    sessions = await fetch_all(admin_engine, "SELECT * FROM identity.sessions ORDER BY created_at")
    assert len(sessions) == 5  # одна от подтверждения почты и четыре входа
    assert len({bytes(s["refresh_hash"]) for s in sessions}) == 5
    assert sessions[-1]["device_label"] == "Firefox на Windows"
    assert sessions[-1]["user_agent"] == "Mozilla/5.0 (test)"
    assert str(sessions[-1]["ip"]) == "127.0.0.1"
    last = await fetch_one(admin_engine, "SELECT last_login_at FROM identity.users")
    assert abs(datetime.now(UTC) - last["last_login_at"]) < timedelta(seconds=30)


def attributes_name(response: httpx.Response) -> str:
    return cookie_attributes(response)["name"]


async def test_login_response_does_not_leak_the_hash_or_the_refresh_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )

    text = response.text
    assert "argon2" not in text
    assert "password" not in text
    assert "refresh" not in text


async def test_wrong_password_and_unknown_login_are_indistinguishable(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    email = user.credentials["email"]

    attempts = [
        await client.post(LOGIN, json={"login": email, "password": "wrong password!"}),
        await client.post(LOGIN, json={"login": "nobody@example.com", "password": PASSWORD}),
        await client.post(LOGIN, json={"login": "nobody_at_all", "password": PASSWORD}),
        await client.post(LOGIN, json={"login": "' OR 1=1 --", "password": PASSWORD}),
    ]

    for response in attempts:
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        assert "set-cookie" not in response.headers
    shapes = [
        {k: v for k, v in r.json().items() if k not in {"request_id", "instance"}} for r in attempts
    ]
    assert all(shape == shapes[0] for shape in shapes)
    assert shapes[0]["code"] == "invalid_credentials"


async def test_unverified_account_learns_its_status_only_with_the_right_password(
    client: httpx.AsyncClient,
) -> None:
    _, body = await register(client)

    right = await client.post(LOGIN, json={"login": body["email"], "password": PASSWORD})
    wrong = await client.post(LOGIN, json={"login": body["email"], "password": "wrong password!"})

    assert (right.status_code, right.json()["code"]) == (403, "email_not_verified")
    assert (wrong.status_code, wrong.json()["code"]) == (401, "invalid_credentials")


async def test_suspended_account_gets_403_with_the_end_of_the_suspension(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(
        admin_engine,
        "UPDATE identity.users SET status = 'suspended', suspended_until = '2030-01-02T03:04:05Z'",
    )

    right = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )
    wrong = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": "wrong password!"}
    )

    assert (right.status_code, right.json()["code"]) == (403, "account_suspended")
    assert right.json()["suspended_until"] == "2030-01-02T03:04:05.000Z"
    assert wrong.status_code == 401  # без верного пароля статус не раскрывается


async def test_banned_account_gets_403(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET status = 'banned'")

    response = await client.post(
        LOGIN, json={"login": user.credentials["username"], "password": PASSWORD}
    )

    assert (response.status_code, response.json()["code"]) == (403, "account_banned")


async def test_account_waiting_for_deletion_can_still_sign_in(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET status = 'deletion_pending'")

    response = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["user"]["status"] == "deletion_pending"


async def test_account_without_a_password_cannot_sign_in_by_password(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET password_hash = NULL")

    response = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )

    assert (response.status_code, response.json()["code"]) == (401, "invalid_credentials")


async def test_login_upgrades_an_outdated_password_hash(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    weak = PasswordService(time_cost=1, memory_cost_kib=4096, parallelism=1, concurrency=1)
    old_hash = await weak.hash(PASSWORD)
    weak.shutdown()
    await execute(admin_engine, "UPDATE identity.users SET password_hash = :hash", hash=old_hash)

    response = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )

    assert response.status_code == 200
    new_hash = (await fetch_one(admin_engine, "SELECT password_hash FROM identity.users"))[
        "password_hash"
    ]
    assert new_hash != old_hash
    assert "m=8192" in new_hash
    again = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )
    assert again.status_code == 200


async def test_failed_logins_leave_no_sessions(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    before = len(await fetch_all(admin_engine, "SELECT id FROM identity.sessions"))

    for _ in range(3):
        await client.post(
            LOGIN, json={"login": user.credentials["email"], "password": "nope nope nope"}
        )

    assert len(await fetch_all(admin_engine, "SELECT id FROM identity.sessions")) == before


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"login": "x"},
        {"password": "x"},
        {"login": "", "password": "x"},
        {"login": "x", "password": "y" * 129},
        {"login": "x", "password": "y", "extra": 1},
    ],
)
async def test_malformed_login_requests_fail_validation(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    response = await client.post(LOGIN, json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

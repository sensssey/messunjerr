"""Пароль: `/auth/password/forgot`, `/auth/password/reset`, `/auth/password/change`."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.services import IdentityServices

from .helpers import (
    LOGIN,
    ME,
    NEW_PASSWORD,
    PASSWORD,
    SignedInUser,
    email_jobs,
    execute,
    fetch_all,
    fetch_one,
    login_again,
    register,
    token_in_email,
    verified_user,
)

FORGOT = "/api/v1/auth/password/forgot"
RESET = "/api/v1/auth/password/reset"
CHANGE = "/api/v1/auth/password/change"


async def forgot(client: httpx.AsyncClient, jobs: InMemoryJobQueue, email: str) -> str:
    """Просит сброс и возвращает токен из поставленного письма."""
    response = await client.post(FORGOT, json={"email": email})
    assert response.status_code == 202, response.text
    return token_in_email(jobs, "reset_password", email)


async def password_hash(engine: AsyncEngine) -> str:
    return (await fetch_one(engine, "SELECT password_hash FROM identity.users"))["password_hash"]


async def can_login(client: httpx.AsyncClient, email: str, password: str) -> bool:
    response = await client.post(LOGIN, json={"login": email, "password": password})
    return response.status_code == 200


def reset_body(token: str, password: str = NEW_PASSWORD) -> dict[str, str]:
    return {"token": token, "new_password": password}


# ----------------------------------------------------------------------------- forgot
async def test_forgot_sends_a_reset_link_to_an_existing_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    email = user.credentials["email"]
    jobs.clear()

    response = await client.post(FORGOT, json={"email": email})

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert response.headers["cache-control"] == "no-store"
    (mail,) = email_jobs(jobs, "reset_password", email)
    context = mail.kwargs["context"]
    token = context["token"]
    assert len(token) == 43
    assert context["reset_url"] == f"http://localhost:3000/reset-password#token={token}"
    assert context["ttl_minutes"] == 60
    row = await fetch_one(
        admin_engine, "SELECT * FROM identity.email_tokens WHERE purpose = 'reset_password'"
    )
    assert bytes(row["token_hash"]) == hashlib.sha256(token.encode()).digest()
    assert timedelta(minutes=59) < row["expires_at"] - datetime.now(UTC) <= timedelta(minutes=60)
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'password.reset_requested'"
    )
    assert str(audit["actor_id"]) == user.user_id


async def test_forgot_answers_the_same_for_everyone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    active = await verified_user(client, jobs)
    _, pending = await register(client)
    banned = await verified_user(client, jobs)
    await execute(
        admin_engine,
        "UPDATE identity.users SET status = 'banned' WHERE email = :email",
        email=banned.credentials["email"],
    )
    jobs.clear()

    answers = [
        await client.post(FORGOT, json={"email": address})
        for address in (
            active.credentials["email"],
            pending["email"],
            banned.credentials["email"],
            "nobody-at-all@example.com",
        )
    ]

    assert {r.status_code for r in answers} == {202}
    assert {r.text for r in answers} == {'{"status":"accepted"}'}
    assert {r.headers["cache-control"] for r in answers} == {"no-store"}
    recipients = {job.kwargs["to"] for job in email_jobs(jobs, "reset_password")}
    assert recipients == {active.credentials["email"], pending["email"]}  # заблокированному не шлём


async def test_a_newer_reset_link_retires_the_older_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    first = await forgot(client, jobs, user.credentials["email"])
    second = await forgot(client, jobs, user.credentials["email"])

    assert first != second
    assert (await client.post(RESET, json=reset_body(first))).status_code == 400
    assert (await client.post(RESET, json=reset_body(second))).status_code == 204


async def test_forgot_survives_a_broken_queue_without_leaving_a_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    jobs.fail_with = ConnectionError("redis is down")

    response = await client.post(FORGOT, json={"email": user.credentials["email"]})

    assert response.status_code == 202  # ответ не зависит ни от адреса, ни от сбоя
    assert (
        await fetch_all(
            admin_engine, "SELECT id FROM identity.email_tokens WHERE purpose = 'reset_password'"
        )
        == []
    )


async def test_forgot_validates_the_address(client: httpx.AsyncClient) -> None:
    assert (await client.post(FORGOT, json={"email": "nope"})).status_code == 422
    assert (await client.post(FORGOT, json={})).status_code == 422


# ----------------------------------------------------------------------------- reset
async def test_reset_sets_the_password_closes_every_session_and_notifies(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)
    email = laptop.credentials["email"]
    token = await forgot(client, jobs, email)
    old_hash = await password_hash(admin_engine)
    jobs.clear()

    response = await client.post(RESET, json=reset_body(token))

    assert response.status_code == 204
    assert response.content == b""
    assert "set-cookie" not in response.headers  # автоматического входа нет
    new_hash = await password_hash(admin_engine)
    assert new_hash != old_hash
    assert new_hash.startswith("$argon2id$")
    assert await can_login(client, email, NEW_PASSWORD)
    assert not await can_login(client, email, PASSWORD)
    for session in (laptop, phone):
        denied = await client.get(ME, headers=session.headers)
        assert (denied.status_code, denied.json()["code"]) == (401, "session_revoked")
        assert await redis_client.exists(f"sess:revoked:{session.session_id}") == 1
    reasons = await fetch_all(
        admin_engine,
        "SELECT revoked_reason FROM identity.sessions WHERE id IN (:a, :b)",
        a=laptop.session_id,
        b=phone.session_id,
    )
    assert {row["revoked_reason"] for row in reasons} == {"password_changed"}
    consumed = await fetch_one(
        admin_engine,
        "SELECT consumed_at FROM identity.email_tokens WHERE purpose = 'reset_password'",
    )
    assert consumed["consumed_at"] is not None
    events = await fetch_all(
        admin_engine, "SELECT event_type, payload FROM platform.outbox ORDER BY id"
    )
    changed = [e for e in events if e["event_type"] == "PasswordChanged"]
    assert changed
    assert changed[-1]["payload"] == {"user_id": laptop.user_id}
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'password.reset'"
    )
    assert audit["data"] == {"revoked_sessions": 2}
    (notice,) = email_jobs(jobs, "password_changed", email)
    assert notice.kwargs["context"]["when"].endswith("UTC")


async def test_the_reset_token_works_only_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    token = await forgot(client, jobs, user.credentials["email"])

    first = await client.post(RESET, json=reset_body(token))
    second = await client.post(RESET, json=reset_body(token, "yet another passphrase 7"))

    assert first.status_code == 204
    assert (second.status_code, second.json()["code"]) == (400, "token_invalid_or_expired")
    assert await can_login(client, user.credentials["email"], NEW_PASSWORD)


async def test_parallel_resets_with_one_token_change_the_password_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    token = await forgot(client, jobs, user.credentials["email"])

    responses = await asyncio.gather(
        *(client.post(RESET, json=reset_body(token, f"parallel passphrase {n}")) for n in range(4))
    )

    assert sorted(r.status_code for r in responses) == [204, 400, 400, 400]


@pytest.mark.parametrize(
    ("password", "reason"),
    [("qwertyuiop", "too_common"), ("kkkkkkkkkkkk", "too_simple")],
)
async def test_a_weak_new_password_is_rejected_and_the_token_survives(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, password: str, reason: str
) -> None:
    user = await verified_user(client, jobs)
    token = await forgot(client, jobs, user.credentials["email"])

    weak = await client.post(RESET, json=reset_body(token, password))

    assert weak.status_code == 422
    (error,) = weak.json()["errors"]
    assert (error["pointer"], error["code"]) == ("/body/new_password", "password_too_weak")
    assert error["meta"] == {"reason": reason}
    assert password not in weak.text
    assert (await client.post(RESET, json=reset_body(token))).status_code == 204


async def test_the_new_password_may_not_equal_the_username_or_email(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs, username="long_unique_name")
    token = await forgot(client, jobs, user.credentials["email"])

    same_name = await client.post(RESET, json=reset_body(token, "long_unique_name"))
    same_email = await client.post(RESET, json=reset_body(token, user.credentials["email"]))

    assert same_name.json()["errors"][0]["meta"] == {"reason": "same_as_username"}
    assert same_email.json()["errors"][0]["meta"] == {"reason": "same_as_email"}


async def test_expired_unknown_and_foreign_purpose_tokens_are_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    expired = await forgot(client, jobs, user.credentials["email"])
    await execute(
        admin_engine, "UPDATE identity.email_tokens SET expires_at = now() - interval '1 second'"
    )
    _, pending = await register(client)
    verification = token_in_email(jobs, "verify_email", pending["email"])

    for token in (expired, "x" * 43, "garbage", verification):
        response = await client.post(RESET, json=reset_body(token))
        assert (response.status_code, response.json()["code"]) == (400, "token_invalid_or_expired")


async def test_reset_proves_mailbox_ownership_and_activates_a_pending_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Тот, кто первым занял чужую почту, не удержит аккаунт: владелец ящика сбросит пароль."""
    _, squatter = await register(client, password="squatter password 1")
    registration_token = token_in_email(jobs, "verify_email", squatter["email"])
    reset_token = await forgot(client, jobs, squatter["email"])

    response = await client.post(RESET, json=reset_body(reset_token, "owner chose this 1"))

    assert response.status_code == 204
    row = await fetch_one(admin_engine, "SELECT status, email_verified_at FROM identity.users")
    assert row["status"] == "active"
    assert row["email_verified_at"] is not None
    assert await can_login(client, squatter["email"], "owner chose this 1")
    assert not await can_login(client, squatter["email"], "squatter password 1")
    # Токен подтверждения от регистрации сквоттера больше не действует.
    stale = await client.post("/api/v1/auth/verify-email", json={"token": registration_token})
    assert stale.status_code == 400
    events = await fetch_all(admin_engine, "SELECT event_type FROM platform.outbox ORDER BY id")
    assert [e["event_type"] for e in events] == [
        "UserRegistered",
        "EmailVerified",
        "PasswordChanged",
    ]


async def test_reset_does_not_unblock_a_banned_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    token = await forgot(client, jobs, user.credentials["email"])
    await execute(admin_engine, "UPDATE identity.users SET status = 'banned'")

    assert (await client.post(RESET, json=reset_body(token))).status_code == 204

    blocked = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": NEW_PASSWORD}
    )
    assert (blocked.status_code, blocked.json()["code"]) == (403, "account_banned")


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"token": "t"}, "/body/new_password"),
        ({"new_password": NEW_PASSWORD}, "/body/token"),
        ({"token": "t", "new_password": "short"}, "/body/new_password"),
        ({"token": "", "new_password": NEW_PASSWORD}, "/body/token"),
    ],
)
async def test_reset_validates_the_body(
    client: httpx.AsyncClient, body: dict[str, str], pointer: str
) -> None:
    response = await client.post(RESET, json=body)

    assert response.status_code == 422
    assert pointer in {e["pointer"] for e in response.json()["errors"]}


# ----------------------------------------------------------------------------- change
async def test_change_keeps_the_current_session_and_closes_the_others(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)
    email = laptop.credentials["email"]
    old_hash = await password_hash(admin_engine)
    jobs.clear()

    response = await client.post(
        CHANGE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=laptop.headers,
    )

    assert response.status_code == 204
    assert await password_hash(admin_engine) != old_hash
    assert (await client.get(ME, headers=laptop.headers)).status_code == 200  # текущая сессия жива
    denied = await client.get(ME, headers=phone.headers)
    assert (denied.status_code, denied.json()["code"]) == (401, "session_revoked")
    assert await redis_client.exists(f"sess:revoked:{phone.session_id}") == 1
    assert await redis_client.exists(f"sess:revoked:{laptop.session_id}") == 0
    assert await can_login(client, email, NEW_PASSWORD)
    assert not await can_login(client, email, PASSWORD)
    assert email_jobs(jobs, "password_changed", email)
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'password.changed'"
    )
    assert audit["data"] == {"revoked_sessions": 1}
    events = await fetch_all(admin_engine, "SELECT event_type FROM platform.outbox ORDER BY id")
    assert events[-1]["event_type"] == "PasswordChanged"


async def test_change_can_leave_other_sessions_open(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    laptop = await verified_user(client, jobs)
    phone = await login_again(client, laptop)

    response = await client.post(
        CHANGE,
        json={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "revoke_other_sessions": False,
        },
        headers=laptop.headers,
    )

    assert response.status_code == 204
    assert (await client.get(ME, headers=phone.headers)).status_code == 200


async def test_a_wrong_current_password_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old_hash = await password_hash(admin_engine)

    response = await client.post(
        CHANGE,
        json={"current_password": "not my password", "new_password": NEW_PASSWORD},
        headers=user.headers,
    )

    assert (response.status_code, response.json()["code"]) == (403, "reauth_failed")
    assert await password_hash(admin_engine) == old_hash
    assert (await client.get(ME, headers=user.headers)).status_code == 200
    failure = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'reauth.failure'"
    )
    assert str(failure["actor_id"]) == user.user_id


@pytest.mark.parametrize(
    ("new_password", "reason"),
    [
        ("qwertyuiop", "too_common"),
        (PASSWORD, "same_as_current"),
        ("x" * 9, None),
    ],
)
async def test_change_applies_the_password_rules(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    new_password: str,
    reason: str | None,
) -> None:
    user = await verified_user(client, jobs)
    old_hash = await password_hash(admin_engine)

    response = await client.post(
        CHANGE,
        json={"current_password": PASSWORD, "new_password": new_password},
        headers=user.headers,
    )

    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["pointer"] == "/body/new_password"
    if reason is not None:
        assert error["meta"] == {"reason": reason}
    assert await password_hash(admin_engine) == old_hash


async def test_change_requires_a_token_and_a_valid_body(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    anonymous = await client.post(
        CHANGE, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )
    empty = await client.post(CHANGE, json={}, headers=user.headers)
    odd_flag = await client.post(
        CHANGE,
        json={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "revoke_other_sessions": "yes",
        },
        headers=user.headers,
    )

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "token_missing")
    assert empty.status_code == 422
    assert odd_flag.status_code == 422


async def test_change_is_unavailable_without_redis(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: SignedInUser = await verified_user(client, jobs)
    old_hash = await password_hash(admin_engine)
    services: IdentityServices = app.state.identity

    async def unreachable(session_id: Any) -> bool:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(services.denylist, "is_revoked", unreachable)

    response = await client.post(
        CHANGE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=user.headers,
    )

    assert (response.status_code, response.json()["code"]) == (503, "service_unavailable")
    assert await password_hash(admin_engine) == old_hash


async def test_the_new_hash_uses_the_current_argon2_parameters(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    await client.post(
        CHANGE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=user.headers,
    )

    new_hash = await password_hash(admin_engine)
    assert new_hash.startswith("$argon2id$")
    assert "m=8192,t=1,p=1" in new_hash
    assert NEW_PASSWORD not in new_hash

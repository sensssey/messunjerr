"""Смена почты: `POST /auth/email/change` и `POST /auth/email/confirm`."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.services import IdentityServices

from .helpers import (
    LOGIN,
    ME,
    PASSWORD,
    SignedInUser,
    bearer,
    do_refresh,
    email_jobs,
    execute,
    fetch_all,
    fetch_one,
    token_in_email,
    verified_user,
)

CHANGE_EMAIL = "/api/v1/auth/email/change"
CONFIRM = "/api/v1/auth/email/confirm"
NEW_EMAIL = "new-address-123@example.com"


async def request_change(
    client: httpx.AsyncClient,
    user: SignedInUser,
    new_email: str = NEW_EMAIL,
    password: str = PASSWORD,
) -> httpx.Response:
    return await client.post(
        CHANGE_EMAIL, json={"new_email": new_email, "password": password}, headers=user.headers
    )


async def current_email(engine: AsyncEngine) -> str:
    row = await fetch_one(engine, "SELECT email::text AS email FROM identity.users")
    return row["email"]


# ----------------------------------------------------------------------------- запрос
async def test_request_sends_a_confirmation_to_the_new_address_and_a_notice_to_the_old(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old_email = user.credentials["email"]
    jobs.clear()

    response = await request_change(client, user)

    assert response.status_code == 202
    assert response.json() == {"status": "confirmation_sent"}
    assert response.headers["cache-control"] == "no-store"
    (confirm,) = email_jobs(jobs, "email_change_confirm", NEW_EMAIL)
    token = confirm.kwargs["context"]["token"]
    assert confirm.kwargs["context"]["confirm_url"] == (
        f"http://localhost:3000/confirm-email#token={token}"
    )
    assert confirm.kwargs["context"]["ttl_minutes"] == 60
    (notice,) = email_jobs(jobs, "email_change_notice", old_email)
    assert notice.kwargs["context"]["new_email_masked"] == "n***@example.com"
    assert NEW_EMAIL not in str(notice.kwargs)  # старый адрес не узнаёт новый целиком
    row = await fetch_one(
        admin_engine, "SELECT * FROM identity.email_tokens WHERE purpose = 'change_email'"
    )
    assert row["new_email"] == NEW_EMAIL
    assert timedelta(minutes=59) < row["expires_at"] - datetime.now(UTC) <= timedelta(minutes=60)
    assert await current_email(admin_engine) == old_email  # пока ничего не изменилось
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'email.change_requested'"
    )
    assert str(audit["actor_id"]) == user.user_id


async def test_the_new_address_is_normalized(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    await request_change(client, user, new_email="  New-Address-123@Example.COM ")

    row = await fetch_one(
        admin_engine,
        "SELECT new_email::text AS new_email FROM identity.email_tokens WHERE purpose = 'change_email'",
    )
    assert row["new_email"] == NEW_EMAIL


async def test_a_wrong_password_is_reauth_failed_and_sends_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    jobs.clear()

    response = await request_change(client, user, password="not my password")

    assert (response.status_code, response.json()["code"]) == (403, "reauth_failed")
    assert jobs.jobs == []
    assert (
        await fetch_all(
            admin_engine, "SELECT id FROM identity.email_tokens WHERE purpose = 'change_email'"
        )
        == []
    )


async def test_a_taken_address_answers_like_success_but_sends_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Занятость адреса по ответу не проверить: `202` и тишина."""
    user = await verified_user(client, jobs)
    other = await verified_user(client, jobs)
    jobs.clear()

    response = await request_change(client, user, new_email=other.credentials["email"].upper())

    assert response.status_code == 202
    assert response.json() == {"status": "confirmation_sent"}
    assert jobs.jobs == []
    assert (
        await fetch_all(
            admin_engine, "SELECT id FROM identity.email_tokens WHERE purpose = 'change_email'"
        )
        == []
    )


async def test_changing_to_the_current_address_is_a_silent_no_op(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    jobs.clear()

    response = await request_change(client, user, new_email=user.credentials["email"].upper())

    assert response.status_code == 202
    assert jobs.jobs == []


async def test_a_newer_request_retires_the_older_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await request_change(client, user, new_email="first-choice@example.com")
    first = token_in_email(jobs, "email_change_confirm", "first-choice@example.com")
    await request_change(client, user, new_email="second-choice@example.com")
    second = token_in_email(jobs, "email_change_confirm", "second-choice@example.com")

    assert (await client.post(CONFIRM, json={"token": first})).status_code == 400
    assert (await client.post(CONFIRM, json={"token": second})).status_code == 204


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"new_email": "nope", "password": PASSWORD}, 422),
        ({"new_email": NEW_EMAIL}, 422),
        ({"password": PASSWORD}, 422),
        ({"new_email": NEW_EMAIL, "password": PASSWORD, "extra": 1}, 422),
    ],
)
async def test_the_request_is_validated(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, body: dict[str, Any], status: int
) -> None:
    user = await verified_user(client, jobs)

    response = await client.post(CHANGE_EMAIL, json=body, headers=user.headers)

    assert response.status_code == status


async def test_the_request_needs_a_token(client: httpx.AsyncClient) -> None:
    response = await client.post(CHANGE_EMAIL, json={"new_email": NEW_EMAIL, "password": PASSWORD})

    assert (response.status_code, response.json()["code"]) == (401, "token_missing")


async def test_the_request_is_unavailable_without_redis(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await verified_user(client, jobs)
    services: IdentityServices = app.state.identity

    async def unreachable(session_id: Any) -> bool:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(services.denylist, "is_revoked", unreachable)

    response = await request_change(client, user)

    assert (response.status_code, response.json()["code"]) == (503, "service_unavailable")


# ----------------------------------------------------------------------------- подтверждение
async def test_confirm_replaces_the_address_and_keeps_every_session(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old_email = user.credentials["email"]
    await request_change(client, user)
    token = token_in_email(jobs, "email_change_confirm", NEW_EMAIL)
    jobs.clear()

    response = await client.post(CONFIRM, json={"token": token})

    assert response.status_code == 204
    assert response.content == b""
    row = await fetch_one(
        admin_engine, "SELECT email::text AS email, email_verified_at FROM identity.users"
    )
    assert row["email"] == NEW_EMAIL
    assert datetime.now(UTC) - row["email_verified_at"] < timedelta(minutes=1)
    # Сессии целы: смена почты их не закрывает.
    me = await client.get(ME, headers=user.headers)
    assert (me.status_code, me.json()["email"]) == (200, NEW_EMAIL)
    assert (await do_refresh(client, user.refresh_token)).status_code == 200
    # Вход теперь по новому адресу.
    old_login = await client.post(LOGIN, json={"login": old_email, "password": PASSWORD})
    new_login = await client.post(LOGIN, json={"login": NEW_EMAIL, "password": PASSWORD})
    assert (old_login.status_code, new_login.status_code) == (401, 200)
    # Прежний адрес получает письмо, адрес в нём замаскирован.
    (notice,) = email_jobs(jobs, "email_changed", old_email)
    assert notice.kwargs["context"]["new_email_masked"] == "n***@example.com"
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'email.changed'"
    )
    assert str(audit["actor_id"]) == user.user_id


async def test_tokens_sent_to_the_old_address_stop_working_after_the_change(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Старый ящик мог быть скомпрометирован: ссылка сброса пароля из него не должна пережить смену."""
    user = await verified_user(client, jobs)
    old_email = user.credentials["email"]
    await client.post("/api/v1/auth/password/forgot", json={"email": old_email})
    stolen_reset = token_in_email(jobs, "reset_password", old_email)
    await request_change(client, user)
    await client.post(
        CONFIRM, json={"token": token_in_email(jobs, "email_change_confirm", NEW_EMAIL)}
    )

    reset = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": stolen_reset, "new_password": "brand new passphrase 77"},
    )

    assert (reset.status_code, reset.json()["code"]) == (400, "token_invalid_or_expired")
    login = await client.post(LOGIN, json={"login": NEW_EMAIL, "password": PASSWORD})
    assert login.status_code == 200  # пароль остался прежним
    live = await fetch_all(
        admin_engine,
        "SELECT purpose FROM identity.email_tokens WHERE consumed_at IS NULL AND expires_at > now()",
    )
    assert live == []


async def test_the_confirmation_token_works_only_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await request_change(client, user)
    token = token_in_email(jobs, "email_change_confirm", NEW_EMAIL)

    first = await client.post(CONFIRM, json={"token": token})
    second = await client.post(CONFIRM, json={"token": token})

    assert (first.status_code, second.status_code) == (204, 400)
    assert second.json()["code"] == "token_invalid_or_expired"


async def test_an_address_taken_in_the_meantime_cannot_be_confirmed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await request_change(client, user)
    token = token_in_email(jobs, "email_change_confirm", NEW_EMAIL)
    squatter = await verified_user(client, jobs)
    await execute(
        admin_engine,
        "UPDATE identity.users SET email = :email WHERE id = :id",
        email=NEW_EMAIL,
        id=squatter.user_id,
    )

    response = await client.post(CONFIRM, json={"token": token})

    assert (response.status_code, response.json()["code"]) == (400, "token_invalid_or_expired")
    mine = await fetch_one(
        admin_engine,
        "SELECT email::text AS email FROM identity.users WHERE id = :id",
        id=user.user_id,
    )
    assert mine["email"] == user.credentials["email"]


async def test_expired_unknown_and_foreign_purpose_tokens_are_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await request_change(client, user)
    expired = token_in_email(jobs, "email_change_confirm", NEW_EMAIL)
    await execute(
        admin_engine,
        "UPDATE identity.email_tokens SET expires_at = now() - interval '1 second' "
        "WHERE purpose = 'change_email'",
    )
    await client.post("/api/v1/auth/password/forgot", json={"email": user.credentials["email"]})
    reset_token = token_in_email(jobs, "reset_password", user.credentials["email"])

    for token in (expired, "x" * 43, "garbage", reset_token):
        response = await client.post(CONFIRM, json={"token": token})
        assert (response.status_code, response.json()["code"]) == (400, "token_invalid_or_expired")


async def test_the_confirmation_endpoint_is_public_and_validated(client: httpx.AsyncClient) -> None:
    assert (await client.post(CONFIRM, json={})).status_code == 422
    assert (await client.post(CONFIRM, json={"token": ""})).status_code == 422
    unauthenticated = await client.post(CONFIRM, json={"token": "x" * 43})
    assert unauthenticated.status_code == 400  # не 401: токена доступа здесь не требуется


async def test_the_old_access_token_keeps_working_with_the_old_claims(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """В токене почты нет, только идентификаторы: смена адреса его не ломает."""
    user = await verified_user(client, jobs)
    await request_change(client, user)
    await client.post(
        CONFIRM, json={"token": token_in_email(jobs, "email_change_confirm", NEW_EMAIL)}
    )

    assert (await client.get(ME, headers=bearer(user.auth["access_token"]))).status_code == 200

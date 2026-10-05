"""`POST /api/v1/auth/register`: создание аккаунта, согласие, пароль, ник, повторная регистрация."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError
from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import (
    PASSWORD,
    fetch_all,
    fetch_one,
    new_credentials,
    register,
    verification_token,
    verified_user,
)

REGISTER = "/api/v1/auth/register"


def errors_of(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "validation_error"
    return response.json()["errors"]


async def user_count(engine: AsyncEngine) -> int:
    return len(await fetch_all(engine, "SELECT id FROM identity.users"))


# ----------------------------------------------------------------------------- успех
async def test_registration_creates_a_pending_account(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    response, body = await register(client)

    assert response.status_code == 201
    assert response.json() == {"status": "verification_sent"}
    assert response.headers["cache-control"] == "no-store"
    user = await fetch_one(
        admin_engine, "SELECT * FROM identity.users WHERE email = :email", email=body["email"]
    )
    assert user["username"] == body["username"]
    assert user["status"] == "pending"
    assert user["role"] == "user"
    assert user["email_verified_at"] is None
    assert user["last_login_at"] is None
    assert user["id"].version == 7
    # ⚖️ упрощённый учёт согласия: версия условий и момент галочки
    assert user["terms_version"] == "2026-10-01"
    assert abs(datetime.now(UTC) - user["terms_accepted_at"]) < timedelta(seconds=30)


async def test_password_is_stored_only_as_an_argon2id_hash(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)
    user = await fetch_one(
        admin_engine,
        "SELECT password_hash FROM identity.users WHERE email = :email",
        email=body["email"],
    )
    assert user["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in user["password_hash"]


async def test_email_and_username_are_stored_lowercase(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    response, _ = await register(client, email="Mixed.Case@Example.COM", username="Mixed_Name")
    assert response.status_code == 201
    user = await fetch_one(
        admin_engine, "SELECT email::text AS email, username::text AS username FROM identity.users"
    )
    assert user == {"email": "mixed.case@example.com", "username": "mixed_name"}


async def test_verification_email_is_queued_with_a_one_time_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)

    (job,) = jobs.named("send_email")
    assert job.queue == "email"
    assert job.kwargs["to"] == body["email"]
    assert job.kwargs["template"] == "verify_email"
    context = job.kwargs["context"]
    token = context["token"]
    assert len(token) == 43
    assert context["verify_url"] == f"http://localhost:3000/verify-email#token={token}"
    assert context["ttl_hours"] == 24

    row = await fetch_one(admin_engine, "SELECT * FROM identity.email_tokens")
    assert row["purpose"] == "verify_email"
    assert bytes(row["token_hash"]) == hashlib.sha256(token.encode()).digest()
    assert token not in json.dumps({k: str(v) for k, v in row.items()})
    assert row["consumed_at"] is None
    remaining = row["expires_at"] - datetime.now(UTC)
    assert timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24)


async def test_registration_event_carries_only_identifiers(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client)

    user = await fetch_one(admin_engine, "SELECT id FROM identity.users")
    event = await fetch_one(admin_engine, "SELECT * FROM platform.outbox")
    assert event["event_type"] == "UserRegistered"
    assert event["topic"] == "mj.identity.user.v1"
    assert event["key"] == str(user["id"])
    assert event["payload"] == {"user_id": str(user["id"])}
    dump = json.dumps({"payload": event["payload"], "headers": event["headers"]})
    assert body["email"] not in dump
    assert body["username"] not in dump


# ----------------------------------------------------------------------------- согласие
@pytest.mark.parametrize("variant", ["false", "absent"])
async def test_terms_checkbox_is_mandatory(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    variant: str,
) -> None:
    body = new_credentials(accept_terms=False)
    if variant == "absent":
        del body["accept_terms"]

    response = await client.post(REGISTER, json=body)

    assert response.status_code == 422
    (error,) = errors_of(response)
    assert error["pointer"] == "/body/accept_terms"
    assert error["code"] == "consent_missing"
    assert await user_count(admin_engine) == 0
    assert jobs.jobs == []


# ----------------------------------------------------------------------------- пароль и ник
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"password": "qwertyuiop"}, "too_common"),
        ({"password": "1234567890"}, "too_common"),
        ({"password": "kkkkkkkkkkkk"}, "too_simple"),
        ({"username": "ivanpetrov77", "password": "IvanPetrov77"}, "same_as_username"),
        (
            {"email": "long.address.here@example.com", "password": "long.address.here@example.com"},
            "same_as_email",
        ),
    ],
)
async def test_weak_password_is_rejected_with_a_reason(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    response, _ = await register(client, **overrides)

    assert response.status_code == 422
    (error,) = errors_of(response)
    assert error["pointer"] == "/body/password"
    assert error["code"] == "password_too_weak"
    assert error["meta"] == {"reason": reason}
    assert jobs.jobs == []


async def test_the_rejected_password_is_never_echoed_back(client: httpx.AsyncClient) -> None:
    response, _ = await register(client, password="qwertyuiop")
    assert "qwertyuiop" not in response.text


async def test_reserved_username_is_rejected(client: httpx.AsyncClient) -> None:
    response, _ = await register(client, username="Admin")

    assert response.status_code == 422
    (error,) = errors_of(response)
    assert (error["pointer"], error["code"]) == ("/body/username", "username_reserved")


async def test_all_problems_are_reported_together(client: httpx.AsyncClient) -> None:
    response, _ = await register(client, username="root", password="qwertyuiop", accept_terms=False)

    assert {(e["pointer"], e["code"]) for e in errors_of(response)} == {
        ("/body/accept_terms", "consent_missing"),
        ("/body/username", "username_reserved"),
        ("/body/password", "password_too_weak"),
    }


async def test_profile_fields_are_not_part_of_the_contract_until_s3(
    client: httpx.AsyncClient,
) -> None:
    response, _ = await register(client, display_name="Иван")

    assert response.status_code == 422
    (error,) = errors_of(response)
    assert (error["pointer"], error["code"]) == ("/body/display_name", "unknown_field")


@pytest.mark.parametrize("same_case", [True, False])
async def test_taken_username_is_409_regardless_of_case(
    client: httpx.AsyncClient, same_case: bool
) -> None:
    first, body = await register(client)
    assert first.status_code == 201
    name = body["username"] if same_case else body["username"].upper()

    response, _ = await register(client, username=name)

    assert response.status_code == 409
    assert response.json()["code"] == "username_taken"


# ----------------------------------------------------------------------------- адрес уже есть
async def test_active_email_gets_the_same_answer_and_nothing_changes(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner = await verified_user(client, jobs)
    before = await fetch_one(admin_engine, "SELECT * FROM identity.users")
    jobs.clear()
    events_before = len(await fetch_all(admin_engine, "SELECT id FROM platform.outbox"))

    response, _ = await register(
        client, email=owner.credentials["email"].upper(), password="a completely different one"
    )

    assert response.status_code == 201
    assert response.json() == {"status": "verification_sent"}
    assert await fetch_one(admin_engine, "SELECT * FROM identity.users") == before
    (job,) = jobs.jobs
    assert job.name == "send_email"
    assert job.kwargs["template"] == "account_exists"
    assert job.kwargs["to"] == owner.credentials["email"]
    assert "token" not in job.kwargs["context"]
    assert len(await fetch_all(admin_engine, "SELECT id FROM platform.outbox")) == events_before


async def test_pending_email_registered_again_replaces_credentials_and_tokens(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Кто первым занял чужую почту, не должен оставить на ней свой пароль (pre-hijacking)."""
    _, attacker = await register(client, password="attacker password 1")
    stale_token = verification_token(jobs, attacker["email"])
    jobs.clear()

    response, owner = await register(
        client, email=attacker["email"], password="owner password 2", username="the_real_owner"
    )

    assert response.status_code == 201
    user = await fetch_one(admin_engine, "SELECT username::text AS username FROM identity.users")
    assert user["username"] == "the_real_owner"
    assert (
        await client.post("/api/v1/auth/verify-email", json={"token": stale_token})
    ).status_code == 400
    fresh_token = verification_token(jobs, attacker["email"])
    verify = await client.post("/api/v1/auth/verify-email", json={"token": fresh_token})
    assert verify.status_code == 200
    ok = await client.post(
        "/api/v1/auth/login", json={"login": owner["email"], "password": "owner password 2"}
    )
    wrong = await client.post(
        "/api/v1/auth/login", json={"login": owner["email"], "password": "attacker password 1"}
    )
    assert (ok.status_code, wrong.status_code) == (200, 401)
    events = await fetch_all(admin_engine, "SELECT event_type FROM platform.outbox ORDER BY id")
    assert [e["event_type"] for e in events].count("UserRegistered") == 1


async def test_pending_account_can_register_again_with_its_own_username(
    client: httpx.AsyncClient,
) -> None:
    first, body = await register(client)
    again = await client.post(REGISTER, json=body)

    assert first.status_code == 201
    assert again.status_code == 201


async def test_taken_username_is_409_whether_or_not_the_email_exists(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """Ответ на занятый ник не выдаёт, есть ли такой адрес."""
    owner = await verified_user(client, jobs)
    other = await verified_user(client, jobs)

    known_email, _ = await register(
        client, email=owner.credentials["email"], username=other.credentials["username"]
    )
    new_email, _ = await register(client, username=other.credentials["username"])

    assert known_email.status_code == new_email.status_code == 409
    strip = {"request_id"}
    assert {k: v for k, v in known_email.json().items() if k not in strip} == {
        k: v for k, v in new_email.json().items() if k not in strip
    }


# ----------------------------------------------------------------------------- гонки
async def test_double_submit_creates_exactly_one_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    body = new_credentials()

    responses = await asyncio.gather(*(client.post(REGISTER, json=body) for _ in range(6)))

    assert {r.status_code for r in responses} == {201}
    assert await user_count(admin_engine) == 1
    active = await fetch_all(
        admin_engine, "SELECT id FROM identity.email_tokens WHERE consumed_at IS NULL"
    )
    assert len(active) == 1  # действует только последний токен
    # И именно он есть в последнем письме.
    token = verification_token(jobs, body["email"])
    assert (
        await client.post("/api/v1/auth/verify-email", json={"token": token})
    ).status_code == 200


async def test_same_username_for_different_emails_in_parallel_gives_one_winner(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    first = new_credentials(username="contested_name")
    second = new_credentials(username="contested_name")

    responses = await asyncio.gather(
        client.post(REGISTER, json=first), client.post(REGISTER, json=second)
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    assert await user_count(admin_engine) == 1


async def test_an_unexpected_queue_error_rolls_everything_back(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    jobs.fail_with = ConnectionError("redis is down")

    response, _ = await register(client)

    assert response.status_code == 500  # не доменная ошибка: общий обработчик
    assert response.json()["code"] == "internal_error"
    assert await user_count(admin_engine) == 0
    assert await fetch_all(admin_engine, "SELECT id FROM platform.outbox") == []
    assert await fetch_all(admin_engine, "SELECT id FROM identity.email_tokens") == []


async def test_unavailable_queue_gives_503_and_rolls_everything_back(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    """Адаптер очереди превращает сбой Redis в `503`: человек повторит запрос, аккаунта не останется."""
    jobs.fail_with = DomainError(
        ErrorCode.SERVICE_UNAVAILABLE, "queue down", headers={"Retry-After": "5"}
    )

    response, _ = await register(client)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "service_unavailable"
    assert await user_count(admin_engine) == 0
    assert await fetch_all(admin_engine, "SELECT id FROM platform.outbox") == []


# ----------------------------------------------------------------------------- вход в API
async def test_malformed_requests_get_problem_json(client: httpx.AsyncClient) -> None:
    broken = await client.post(
        REGISTER, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert broken.status_code == 400
    assert broken.json()["code"] == "invalid_request"

    form = await client.post(REGISTER, data={"email": "a@example.com"})
    assert form.status_code == 415
    assert form.json()["code"] == "unsupported_media_type"

    empty = await client.post(REGISTER, json={})
    assert empty.status_code == 422
    assert {e["pointer"] for e in errors_of(empty)} == {
        "/body/email",
        "/body/username",
        "/body/password",
    }


async def test_invalid_attempts_leave_no_account_behind(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    await register(client, password="qwertyuiop")
    await register(client, accept_terms=False)
    await register(client, username="admin")
    assert await user_count(admin_engine) == 0

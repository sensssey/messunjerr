"""`POST /auth/refresh`: ротация, окно гонки двух вкладок, повторное использование, сроки, CSRF."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import (
    CSRF,
    ME,
    REFRESH,
    REFRESH_COOKIE,
    bearer,
    cookie_header,
    cookie_token,
    do_refresh,
    email_jobs,
    execute,
    fetch_all,
    fetch_one,
    refresh_headers,
    verified_user,
)


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def session_row(engine: AsyncEngine) -> dict[str, Any]:
    return await fetch_one(engine, "SELECT * FROM identity.sessions")


# ----------------------------------------------------------------------------- ротация
async def test_refresh_rotates_the_token_and_issues_a_new_access_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    first = user.refresh_token

    response = await do_refresh(client, first)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "session_id", "user"}
    assert body["session_id"] == user.session_id
    assert body["expires_in"] == 1200
    assert body["user"]["id"] == user.user_id
    assert body["access_token"] != user.auth["access_token"]

    second = cookie_token(response)
    assert second != first
    set_cookie = response.headers["set-cookie"].lower()
    for attribute in ("httponly", "secure", "samesite=strict", "path=/api/v1/auth", "max-age="):
        assert attribute in set_cookie

    row = await session_row(admin_engine)
    assert bytes(row["refresh_hash"]) == digest(second)
    assert bytes(row["prev_refresh_hash"]) == digest(first)
    assert abs(datetime.now(UTC) - row["rotated_at"]) < timedelta(seconds=30)
    assert row["revoked_at"] is None

    assert (await client.get(ME, headers=bearer(body["access_token"]))).status_code == 200


async def test_each_refresh_rotates_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    token = user.refresh_token
    seen = {token}

    for _ in range(3):
        # Каждый раз прошлая ротация «состарена»: иначе второй запрос попал бы в окно гонки.
        await execute(
            admin_engine, "UPDATE identity.sessions SET rotated_at = now() - interval '1 minute'"
        )
        response = await do_refresh(client, token)
        assert response.status_code == 200
        token = cookie_token(response)
        assert token not in seen
        seen.add(token)

    assert bytes((await session_row(admin_engine))["refresh_hash"]) == digest(token)


async def test_the_sliding_term_is_30_days_but_never_beyond_the_absolute_limit(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    ordinary = await do_refresh(client, user.refresh_token)
    row = await session_row(admin_engine)
    assert (
        timedelta(days=29, hours=23) < row["expires_at"] - datetime.now(UTC) <= timedelta(days=30)
    )
    ordinary_max_age = int(
        ordinary.headers["set-cookie"].lower().split("max-age=")[1].split(";")[0]
    )
    assert 29 * 24 * 3600 < ordinary_max_age <= 30 * 24 * 3600

    await execute(
        admin_engine,
        "UPDATE identity.sessions SET absolute_expires_at = now() + interval '2 days', "
        "rotated_at = now() - interval '1 minute'",
    )
    capped = await do_refresh(client, cookie_token(ordinary))
    assert capped.status_code == 200
    row = await session_row(admin_engine)
    assert row["expires_at"] == row["absolute_expires_at"]
    max_age = int(capped.headers["set-cookie"].lower().split("max-age=")[1].split(";")[0])
    assert max_age <= 2 * 24 * 3600


async def test_last_seen_is_updated_on_refresh(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(
        admin_engine, "UPDATE identity.sessions SET last_seen_at = now() - interval '2 hours'"
    )

    await do_refresh(client, user.refresh_token)

    row = await session_row(admin_engine)
    assert datetime.now(UTC) - row["last_seen_at"] < timedelta(minutes=1)


# ----------------------------------------------------------------------------- окно гонки
async def test_second_tab_inside_the_window_gets_only_an_access_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    first = await do_refresh(client, user.refresh_token)
    assert first.status_code == 200
    rotated_hash = (await session_row(admin_engine))["refresh_hash"]

    # Вторая вкладка прислала старый токен: прошло меньше 10 секунд.
    second = await do_refresh(client, user.refresh_token)

    assert second.status_code == 200
    assert "set-cookie" not in second.headers  # браузер уже получил свежую cookie из первого ответа
    assert second.json()["session_id"] == user.session_id
    assert (await client.get(ME, headers=bearer(second.json()["access_token"]))).status_code == 200
    row = await session_row(admin_engine)
    assert row["refresh_hash"] == rotated_hash  # повторная ротация не произошла
    assert row["revoked_at"] is None


async def test_parallel_refreshes_with_one_token_all_succeed_and_rotate_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Гонка двух вкладок по-настоящему: запросы идут параллельно, а не один за другим."""
    user = await verified_user(client, jobs)

    responses = await asyncio.gather(*(do_refresh(client, user.refresh_token) for _ in range(6)))

    assert {r.status_code for r in responses} == {200}
    with_cookie = [r for r in responses if "set-cookie" in r.headers]
    assert len(with_cookie) == 1  # токен ротировался ровно один раз
    row = await session_row(admin_engine)
    assert bytes(row["refresh_hash"]) == digest(cookie_token(with_cookie[0]))
    assert bytes(row["prev_refresh_hash"]) == digest(user.refresh_token)
    assert row["revoked_at"] is None
    for response in responses:
        me = await client.get(ME, headers=bearer(response.json()["access_token"]))
        assert me.status_code == 200


# ----------------------------------------------------------------------------- повторное использование
async def test_reuse_after_the_window_closes_the_session_and_warns_the_owner(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    user = await verified_user(client, jobs)
    stolen = user.refresh_token
    rotated = await do_refresh(client, stolen)  # законный владелец обновил токен
    legit = cookie_token(rotated)
    await execute(
        admin_engine, "UPDATE identity.sessions SET rotated_at = now() - interval '11 seconds'"
    )
    jobs.clear()

    thief = await do_refresh(client, stolen)  # вор предъявляет старый токен позже окна

    assert thief.status_code == 401
    assert thief.json()["code"] == "refresh_reused"
    assert thief.headers["content-type"] == "application/problem+json"
    cleared = thief.headers["set-cookie"].lower()
    assert f"{REFRESH_COOKIE.lower()}=" in cleared
    assert "max-age=0" in cleared
    row = await session_row(admin_engine)
    assert row["revoked_at"] is not None
    assert row["revoked_reason"] == "reuse_detected"
    assert await redis_client.exists(f"sess:revoked:{user.session_id}") == 1
    # Уже выданный access-токен перестаёт приниматься сразу, а не через 20 минут.
    revoked = await client.get(ME, headers=bearer(rotated.json()["access_token"]))
    assert (revoked.status_code, revoked.json()["code"]) == (401, "session_revoked")
    # Законный токен тоже мёртв: сессия закрыта целиком, нужен новый вход.
    assert (await do_refresh(client, legit)).json()["code"] == "refresh_invalid"
    # След остался в аудите, владелец получил письмо.
    audit = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'refresh.reuse_detected'"
    )
    assert str(audit["actor_id"]) == user.user_id
    assert audit["target_type"] == "session"
    assert audit["target_id"] == user.session_id
    (mail,) = email_jobs(jobs, "refresh_reuse", user.credentials["email"])
    assert mail.queue == "email"


async def test_reuse_is_recorded_even_when_the_mail_queue_is_down(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Отзыв сессии не зависит от почты: письмо лишь лучшее усилие после коммита."""
    user = await verified_user(client, jobs)
    await do_refresh(client, user.refresh_token)
    await execute(
        admin_engine, "UPDATE identity.sessions SET rotated_at = now() - interval '11 seconds'"
    )
    jobs.fail_with = ConnectionError("redis is down")

    response = await do_refresh(client, user.refresh_token)

    assert response.json()["code"] == "refresh_reused"
    assert (await session_row(admin_engine))["revoked_reason"] == "reuse_detected"


async def test_a_token_from_two_rotations_ago_is_simply_unknown(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    first = user.refresh_token
    second = cookie_token(await do_refresh(client, first))
    await execute(
        admin_engine, "UPDATE identity.sessions SET rotated_at = now() - interval '1 minute'"
    )
    await do_refresh(client, second)

    response = await do_refresh(client, first)

    assert (response.status_code, response.json()["code"]) == (401, "refresh_invalid")
    assert (await session_row(admin_engine))["revoked_at"] is None  # это не кража, сессия жива


# ----------------------------------------------------------------------------- ошибки токена
async def test_missing_cookie(client: httpx.AsyncClient) -> None:
    response = await client.post(REFRESH, headers=CSRF)

    assert (response.status_code, response.json()["code"]) == (401, "refresh_missing")


@pytest.mark.parametrize("token", ["x" * 43, "garbage", "' OR 1=1 --"])
async def test_unknown_token_is_invalid_and_the_cookie_is_cleared(
    client: httpx.AsyncClient, token: str
) -> None:
    response = await do_refresh(client, token)

    assert (response.status_code, response.json()["code"]) == (401, "refresh_invalid")
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_token_of_a_revoked_session_is_invalid(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(
        admin_engine, "UPDATE identity.sessions SET revoked_at = now(), revoked_reason = 'logout'"
    )

    response = await do_refresh(client, user.refresh_token)

    assert (response.status_code, response.json()["code"]) == (401, "refresh_invalid")


@pytest.mark.parametrize("column", ["expires_at", "absolute_expires_at"])
async def test_expired_session_is_refresh_expired(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, column: str
) -> None:
    user = await verified_user(client, jobs)
    # Имя столбца берётся из параметров теста выше, а не из ввода.
    await execute(
        admin_engine,
        f"UPDATE identity.sessions SET {column} = now() - interval '1 second'",
    )

    response = await do_refresh(client, user.refresh_token)

    assert (response.status_code, response.json()["code"]) == (401, "refresh_expired")
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_session_of_a_deleted_user_is_gone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "DELETE FROM identity.users")

    response = await do_refresh(client, user.refresh_token)

    assert response.json()["code"] == "refresh_invalid"
    assert await fetch_all(admin_engine, "SELECT id FROM identity.sessions") == []


@pytest.mark.parametrize(
    ("status", "code"), [("suspended", "account_suspended"), ("banned", "account_banned")]
)
async def test_blocked_account_cannot_refresh_and_the_token_is_not_spent(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    status: str,
    code: str,
) -> None:
    user = await verified_user(client, jobs)
    before = await session_row(admin_engine)
    await execute(admin_engine, "UPDATE identity.users SET status = :status", status=status)

    response = await do_refresh(client, user.refresh_token)

    assert (response.status_code, response.json()["code"]) == (403, code)
    assert (await session_row(admin_engine))["refresh_hash"] == before["refresh_hash"]


async def test_an_account_waiting_for_deletion_can_still_refresh(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET status = 'deletion_pending'")

    response = await do_refresh(client, user.refresh_token)

    assert response.status_code == 200
    assert response.json()["user"]["status"] == "deletion_pending"


# ----------------------------------------------------------------------------- CSRF
@pytest.mark.parametrize(
    "headers",
    [
        {},  # нет X-Requested-With
        {"X-Requested-With": "XMLHttpRequest"},
        {"X-Requested-With": "messunjerr", "Origin": "https://evil.example"},
        {"X-Requested-With": "messunjerr", "Origin": "null"},
        {"X-Requested-With": "messunjerr", "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["no-header", "wrong-value", "foreign-origin", "null-origin", "cross-site"],
)
async def test_cookie_endpoint_rejects_requests_that_fail_the_csrf_checks(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    headers: dict[str, str],
) -> None:
    user = await verified_user(client, jobs)
    before = await session_row(admin_engine)

    response = await client.post(REFRESH, headers={**headers, **cookie_header(user.refresh_token)})

    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")
    assert (await session_row(admin_engine))["refresh_hash"] == before["refresh_hash"]  # токен цел
    assert "set-cookie" not in response.headers


async def test_requests_from_the_client_origin_and_without_origin_are_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    with_origin = await client.post(
        REFRESH,
        headers=refresh_headers(user.refresh_token, **{"Sec-Fetch-Site": "same-origin"}),
    )
    await execute(
        admin_engine, "UPDATE identity.sessions SET rotated_at = now() - interval '1 minute'"
    )
    without_origin = await client.post(
        REFRESH,
        headers={
            "X-Requested-With": "messunjerr",
            **cookie_header(cookie_token(with_origin)),
        },
    )

    assert (with_origin.status_code, without_origin.status_code) == (200, 200)

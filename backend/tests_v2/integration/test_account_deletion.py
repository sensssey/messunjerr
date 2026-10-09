"""S3-06: `DELETE /me`, `POST /me/restore`, статус `deletion_pending` и ограничение `account_deletion_pending`."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.services import IdentityServices
from messunjerr.settings import Settings

from .helpers import (
    ME,
    PASSWORD,
    SignedInUser,
    bearer,
    client_with,
    do_refresh,
    email_jobs,
    execute,
    fetch_all,
    fetch_one,
    limited_client,
    login_again,
    verified_user,
)

DELETE_ME = "/api/v1/me"
RESTORE = "/api/v1/me/restore"
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


async def delete_account(
    client: httpx.AsyncClient, user: SignedInUser, password: str | None = PASSWORD
) -> httpx.Response:
    body = {"password": password} if password is not None else {}
    return await client.request("DELETE", DELETE_ME, json=body, headers=user.headers)


async def restore(client: httpx.AsyncClient, user: SignedInUser) -> httpx.Response:
    return await client.post(RESTORE, headers=user.headers)


async def status_of(engine: AsyncEngine) -> dict[str, Any]:
    return await fetch_one(engine, "SELECT status, deletion_scheduled_at, role FROM identity.users")


def flag_key(user: SignedInUser) -> str:
    return f"acct:deletion:{user.user_id}"


# ----------------------------------------------------------------------------- запрос удаления
async def test_deleting_schedules_the_removal_in_fourteen_days(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    before = datetime.now(UTC)

    response = await delete_account(client, user)

    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    scheduled = response.json()["deletion_scheduled_at"]
    assert RFC3339_UTC.fullmatch(scheduled)
    when = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    assert (
        timedelta(days=14) - timedelta(seconds=5) <= when - before <= timedelta(days=14, seconds=30)
    )
    row = await status_of(admin_engine)
    assert row["status"] == "deletion_pending"
    assert row["deletion_scheduled_at"] - when < timedelta(milliseconds=1)
    assert when - row["deletion_scheduled_at"] < timedelta(milliseconds=1)


async def test_me_still_answers_and_shows_the_status(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)

    response = await client.get(ME, headers=user.headers)

    assert response.status_code == 200
    assert response.json()["status"] == "deletion_pending"
    assert response.json()["profile"]["display_name"] == user.credentials["username"]


async def test_the_request_leaves_an_event_an_audit_entry_and_a_flag(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    user = await verified_user(client, jobs)

    await delete_account(client, user)

    event = await fetch_one(
        admin_engine, "SELECT * FROM platform.outbox WHERE event_type = 'UserDeletionRequested'"
    )
    assert event["topic"] == "mj.identity.user.v1"
    assert event["key"] == user.user_id
    assert event["payload"] == {
        "user_id": user.user_id
    }  # только идентификатор, без персональных данных
    entry = await fetch_one(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'account.deletion_requested'"
    )
    assert str(entry["actor_id"]) == str(entry["target_id"]) == user.user_id
    assert entry["data"] == {"revoked_sessions": 0, "grace_days": 14}
    assert user.credentials["email"] not in str(entry)
    assert await redis_client.get(flag_key(user)) == "1"
    assert 0 < await redis_client.ttl(flag_key(user)) <= 1500  # токен 20 минут и запас 5 минут


async def test_the_owner_is_told_by_email_when_the_data_will_be_removed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    scheduled = (await delete_account(client, user)).json()["deletion_scheduled_at"]

    (job,) = email_jobs(jobs, "account_deletion_requested", user.credentials["email"])

    assert job.queue == "email"
    context = job.kwargs["context"]
    assert context["grace_days"] == 14
    assert context["login_url"] == "http://localhost:3000/login"
    assert context["reset_url"] == "http://localhost:3000/forgot-password"
    when = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    assert context["scheduled_at"] == when.strftime("%d.%m.%Y %H:%M UTC")


async def test_the_grace_period_is_a_setting(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with client_with(test_settings, jobs, account_deletion_grace_days=3) as short:
        user = await verified_user(short, jobs)

        response = await delete_account(short, user)

        when = datetime.fromisoformat(
            response.json()["deletion_scheduled_at"].replace("Z", "+00:00")
        )
        assert timedelta(days=2, hours=23) < when - datetime.now(UTC) <= timedelta(days=3)
        (job,) = email_jobs(jobs, "account_deletion_requested")
        assert job.kwargs["context"]["grace_days"] == 3


# ----------------------------------------------------------------------------- подтверждение пароля
async def test_a_wrong_password_changes_nothing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    user = await verified_user(client, jobs)

    response = await delete_account(client, user, "not my password at all")

    assert response.status_code == 403
    assert response.json()["code"] == "reauth_failed"
    row = await status_of(admin_engine)
    assert (row["status"], row["deletion_scheduled_at"]) == ("active", None)
    assert (
        await fetch_all(
            admin_engine, "SELECT 1 FROM platform.outbox WHERE event_type = 'UserDeletionRequested'"
        )
        == []
    )
    assert email_jobs(jobs, "account_deletion_requested") == []
    assert await redis_client.exists(flag_key(user)) == 0
    failure = await fetch_one(
        admin_engine, "SELECT actor_id FROM platform.audit_log WHERE action = 'reauth.failure'"
    )
    assert str(failure["actor_id"]) == user.user_id
    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 200


@pytest.mark.parametrize("body", [{}, {"password": None}])
async def test_the_password_is_required_for_an_account_that_has_one(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    body: dict[str, Any],
) -> None:
    user = await verified_user(client, jobs)

    response = await client.request("DELETE", DELETE_ME, json=body, headers=user.headers)

    assert response.status_code == 422
    assert [(e["pointer"], e["code"]) for e in response.json()["errors"]] == [
        ("/body/password", "required")
    ]
    assert (await status_of(admin_engine))["status"] == "active"


async def test_a_request_without_any_body_is_the_same_as_an_empty_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.request("DELETE", DELETE_ME, headers=user.headers)

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/body/password"


async def test_an_empty_password_and_unknown_fields_are_rejected(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    empty = await delete_account(client, user, "")
    extra = await client.request(
        "DELETE", DELETE_ME, json={"password": PASSWORD, "force": True}, headers=user.headers
    )

    assert [e["code"] for e in empty.json()["errors"]] == ["string_too_short"]
    assert [(e["pointer"], e["code"]) for e in extra.json()["errors"]] == [
        ("/body/force", "unknown_field")
    ]


async def test_a_password_with_spaces_at_the_edges_is_compared_as_typed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    response = await delete_account(client, user, f"  {PASSWORD}  ")

    assert response.status_code == 403  # пароль не обрезается, как и при входе
    assert (await status_of(admin_engine))["status"] == "active"


@pytest.mark.parametrize("role", ["moderator", "admin"])
async def test_staff_must_give_up_the_role_before_deleting_the_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, role: str
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET role = :r", r=role)

    response = await delete_account(client, user)

    assert response.status_code == 409
    assert response.json()["code"] == "role_must_be_revoked"
    assert (await status_of(admin_engine))["status"] == "active"
    assert email_jobs(jobs, "account_deletion_requested") == []


async def test_a_wrong_password_is_checked_before_the_role_is_revealed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET role = 'admin'")

    response = await delete_account(client, user, "wrong wrong wrong")

    assert response.json()["code"] == "reauth_failed"


# ----------------------------------------------------------------------------- сессии
async def test_other_sessions_are_closed_and_the_current_one_stays(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    first = await verified_user(client, jobs)
    second = await login_again(client, first)
    third = await login_again(client, first)

    response = await delete_account(client, first)

    assert response.status_code == 202
    for other in (second, third):
        denied = await client.get(ME, headers=other.headers)
        assert (denied.status_code, denied.json()["code"]) == (401, "session_revoked")
        refreshed = await do_refresh(client, other.refresh_token)
        assert (refreshed.status_code, refreshed.json()["code"]) == (401, "refresh_invalid")
    assert (await client.get(ME, headers=first.headers)).status_code == 200
    rows = await fetch_all(
        admin_engine, "SELECT id, revoked_at, revoked_reason FROM identity.sessions"
    )
    reasons = {str(row["id"]): row["revoked_reason"] for row in rows if row["revoked_at"]}
    assert reasons == {
        second.session_id: "deletion_requested",
        third.session_id: "deletion_requested",
    }
    assert first.session_id not in reasons


# ----------------------------------------------------------------------------- ограничение 403
GUARDED: list[tuple[str, str, dict[str, Any] | None]] = [
    ("PATCH", "/api/v1/me/profile", {"bio": "x"}),
    ("GET", "/api/v1/me/privacy", None),
    ("PATCH", "/api/v1/me/privacy", {"dm_policy": "nobody"}),
    ("PATCH", "/api/v1/me/username", {"username": "brand_new_name"}),
    ("GET", "/api/v1/users/{me}", None),
    ("GET", "/api/v1/auth/sessions", None),
    ("DELETE", "/api/v1/auth/sessions/{session}", None),
    ("POST", "/api/v1/auth/logout-all", {"password": PASSWORD}),
    (
        "POST",
        "/api/v1/auth/password/change",
        {"current_password": PASSWORD, "new_password": "another sturdy passphrase 42"},
    ),
    ("POST", "/api/v1/auth/email/change", {"new_email": "new@example.com", "password": PASSWORD}),
    ("DELETE", "/api/v1/me", {"password": PASSWORD}),
]


@pytest.mark.parametrize(("method", "path", "body"), GUARDED)
async def test_everything_but_me_restore_and_logout_is_closed_to_an_account_awaiting_deletion(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)

    response = await client.request(
        method,
        path.format(me=user.user_id, session=user.session_id),
        json=body,
        headers=user.headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "account_deletion_pending"
    assert response.headers["content-type"] == "application/problem+json"
    assert "www-authenticate" not in response.headers  # это не ошибка токена


async def test_the_closure_does_not_touch_other_people(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    leaving = await verified_user(client, jobs)
    staying = await verified_user(client, jobs)
    await delete_account(client, leaving)

    assert (await client.get("/api/v1/me/privacy", headers=staying.headers)).status_code == 200
    assert (
        await client.patch("/api/v1/me/profile", json={"bio": "ok"}, headers=staying.headers)
    ).status_code == 200


async def test_logout_still_works_for_an_account_awaiting_deletion(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)

    response = await client.post(
        "/api/v1/auth/logout",
        headers={
            "X-Requested-With": "messunjerr",
            "Origin": "http://localhost:3000",
            "Cookie": f"__Secure-mj_refresh={user.refresh_token}",
        },
    )

    assert response.status_code == 204
    after = await client.get(
        ME, headers=user.headers
    )  # выход закрыл сессию: токен больше не годится
    assert (after.status_code, after.json()["code"]) == (401, "session_revoked")


async def test_the_account_can_sign_in_again_but_stays_restricted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, redis_client: Redis
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    await redis_client.flushdb()  # pyright: ignore[reportUnknownMemberType]

    again = await login_again(client, user)

    assert again.auth["user"]["status"] == "deletion_pending"  # клиент предложит восстановление
    assert await redis_client.get(flag_key(user)) == "1"  # вход подтвердил признак
    denied = await client.get("/api/v1/me/privacy", headers=again.headers)
    assert (denied.status_code, denied.json()["code"]) == (403, "account_deletion_pending")
    assert (await client.get(ME, headers=again.headers)).status_code == 200


async def test_refresh_keeps_the_restriction_and_restores_a_lost_flag(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, redis_client: Redis
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    await redis_client.flushdb()  # pyright: ignore[reportUnknownMemberType]

    # Redis потерял признак: уже выданный токен до конца своей жизни проходит (как с denylist) ...
    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 200
    refreshed = await do_refresh(client, user.refresh_token)

    # ... но новый токен выдаётся с восстановленным признаком.
    assert refreshed.status_code == 200
    assert refreshed.json()["user"]["status"] == "deletion_pending"
    assert await redis_client.get(flag_key(user)) == "1"
    new_headers = bearer(refreshed.json()["access_token"])
    denied = await client.get("/api/v1/me/privacy", headers=new_headers)
    assert (denied.status_code, denied.json()["code"]) == (403, "account_deletion_pending")


async def test_a_repeated_request_restores_a_lost_flag_without_a_second_deletion(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    redis_client: Redis,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    first = await delete_account(client, user)
    await redis_client.flushdb()  # pyright: ignore[reportUnknownMemberType]
    # Признак потерян: уже выданный токен проходит ограничение (как и с denylist) ...
    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 200

    again = await delete_account(client, user)

    # ... а повторный запрос возвращает прежний срок и ставит признак заново, не удаляя аккаунт дважды.
    assert again.status_code == 202
    assert again.json() == first.json()
    assert await redis_client.get(flag_key(user)) == "1"
    denied = await client.get("/api/v1/me/privacy", headers=user.headers)
    assert (denied.status_code, denied.json()["code"]) == (403, "account_deletion_pending")
    assert len(email_jobs(jobs, "account_deletion_requested")) == 1
    events = await fetch_all(
        admin_engine, "SELECT 1 FROM platform.outbox WHERE event_type = 'UserDeletionRequested'"
    )
    assert len(events) == 1


async def test_the_restriction_lapses_in_general_endpoints_when_redis_is_down(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Как и denylist сессий: при недоступном Redis общие ручки работают, чувствительные дают 503 (4.7)."""
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    services: IdentityServices = app.state.identity

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(services.denylist, "access_state", unreachable)

    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 200
    sensitive = await client.get("/api/v1/auth/sessions", headers=user.headers)
    assert (sensitive.status_code, sensitive.json()["code"]) == (503, "service_unavailable")


async def test_deleting_the_account_needs_redis_like_other_sensitive_operations(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    app: FastAPI,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await verified_user(client, jobs)
    services: IdentityServices = app.state.identity

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(services.denylist, "access_state", unreachable)

    response = await delete_account(client, user)

    assert (response.status_code, response.json()["code"]) == (503, "service_unavailable")
    assert (await status_of(admin_engine))["status"] == "active"


# ----------------------------------------------------------------------------- профиль скрыт
async def test_the_profile_disappears_for_others_and_comes_back_with_the_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    leaving = await verified_user(client, jobs)
    watcher = await verified_user(client, jobs)
    profile_url = f"/api/v1/users/{leaving.credentials['username']}"
    assert (await client.get(profile_url, headers=watcher.headers)).status_code == 200

    await delete_account(client, leaving)
    hidden = await client.get(profile_url, headers=watcher.headers)
    await restore(client, leaving)
    back = await client.get(profile_url, headers=watcher.headers)

    assert (hidden.status_code, hidden.json()["code"]) == (404, "not_found")
    assert back.status_code == 200


# ----------------------------------------------------------------------------- восстановление
async def test_restore_cancels_the_deletion_and_returns_me(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)

    response = await restore(client, user)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "active"
    assert body["id"] == user.user_id
    assert set(body) == set((await client.get(ME, headers=user.headers)).json())
    row = await status_of(admin_engine)
    assert (row["status"], row["deletion_scheduled_at"]) == ("active", None)
    assert await redis_client.exists(flag_key(user)) == 0
    entry = await fetch_one(
        admin_engine, "SELECT actor_id FROM platform.audit_log WHERE action = 'account.restored'"
    )
    assert str(entry["actor_id"]) == user.user_id


async def test_after_a_restore_everything_works_again_with_the_same_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 403

    await restore(client, user)

    assert (await client.get("/api/v1/me/privacy", headers=user.headers)).status_code == 200
    assert (
        await client.patch("/api/v1/me/profile", json={"bio": "снова здесь"}, headers=user.headers)
    ).status_code == 200


async def test_restore_does_not_bring_back_closed_sessions(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    first = await verified_user(client, jobs)
    second = await login_again(client, first)
    await delete_account(client, first)

    await restore(client, first)

    assert (await client.get(ME, headers=second.headers)).status_code == 401


async def test_a_second_deletion_after_a_restore_starts_a_new_period(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    first = (await delete_account(client, user)).json()["deletion_scheduled_at"]
    await restore(client, user)

    second = await delete_account(client, user)

    assert second.status_code == 202
    assert second.json()["deletion_scheduled_at"] >= first
    assert len(email_jobs(jobs, "account_deletion_requested")) == 2


async def test_restore_of_an_active_account_is_a_conflict(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await restore(client, user)

    assert response.status_code == 409
    assert response.json()["code"] == "not_pending_deletion"


async def test_restore_twice_is_a_conflict_the_second_time(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)

    assert (await restore(client, user)).status_code == 200
    again = await restore(client, user)

    assert (again.status_code, again.json()["code"]) == (409, "not_pending_deletion")


async def test_nothing_can_be_restored_after_the_grace_period(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    await execute(
        admin_engine,
        "UPDATE identity.users SET deletion_scheduled_at = now() - interval '1 second'",
    )

    response = await restore(client, user)

    assert (response.status_code, response.json()["code"]) == (409, "not_pending_deletion")
    assert "grace period" in response.json()["detail"]
    assert (await status_of(admin_engine))["status"] == "deletion_pending"


async def test_the_last_moment_before_the_deadline_still_allows_a_restore(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await delete_account(client, user)
    await execute(
        admin_engine,
        "UPDATE identity.users SET deletion_scheduled_at = now() + interval '1 minute'",
    )

    assert (await restore(client, user)).status_code == 200


async def test_restore_needs_a_token(client: httpx.AsyncClient) -> None:
    response = await client.post(RESTORE)

    assert (response.status_code, response.json()["code"]) == (401, "token_missing")


# ----------------------------------------------------------------------------- гонки
async def test_two_simultaneous_requests_schedule_one_deletion(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    responses = await asyncio.gather(*(delete_account(client, user) for _ in range(4)))

    accepted = [r for r in responses if r.status_code == 202]
    assert accepted, [r.text for r in responses]
    assert {r.json()["deletion_scheduled_at"] for r in accepted} == {
        accepted[0].json()["deletion_scheduled_at"]
    }
    # Кто пришёл после появления признака, получил 403: срок не сдвинулся ни у кого.
    assert {r.status_code for r in responses} <= {202, 403}
    assert (
        len(
            await fetch_all(
                admin_engine,
                "SELECT 1 FROM platform.outbox WHERE event_type = 'UserDeletionRequested'",
            )
        )
        == 1
    )
    assert len(email_jobs(jobs, "account_deletion_requested")) == 1
    assert (
        len(
            await fetch_all(
                admin_engine,
                "SELECT 1 FROM platform.audit_log WHERE action = 'account.deletion_requested'",
            )
        )
        == 1
    )


# ----------------------------------------------------------------------------- аккаунт без пароля
async def test_an_account_without_a_password_confirms_with_a_fresh_session(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Вход через OAuth (S21) пароля не создаёт: хватает сессии не старше пяти минут (5.3)."""
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET password_hash = NULL")

    response = await client.request("DELETE", DELETE_ME, headers=user.headers)

    assert response.status_code == 202, response.text
    assert (await status_of(admin_engine))["status"] == "deletion_pending"


async def test_an_account_without_a_password_needs_a_session_younger_than_five_minutes(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET password_hash = NULL")
    await execute(
        admin_engine, "UPDATE identity.sessions SET created_at = now() - interval '6 minutes'"
    )

    response = await client.request("DELETE", DELETE_ME, headers=user.headers)

    assert (response.status_code, response.json()["code"]) == (403, "reauth_failed")
    assert (await status_of(admin_engine))["status"] == "active"


async def test_a_password_in_the_body_does_not_help_an_account_without_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET password_hash = NULL")
    await execute(
        admin_engine, "UPDATE identity.sessions SET created_at = now() - interval '1 hour'"
    )

    response = await delete_account(client, user, "any password at all")

    assert response.status_code == 403
    assert response.json()["code"] == "reauth_failed"


# ----------------------------------------------------------------------------- лимиты и документация
async def test_restore_shares_the_write_limit(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, api_write=2) as (_, http):
        user = await verified_user(http, jobs)

        statuses = [
            (await http.post(RESTORE, headers=user.headers)).status_code,  # 409: расходует токен
            (await http.post(RESTORE, headers=user.headers)).status_code,
            (await http.post(RESTORE, headers=user.headers)).status_code,
        ]

        assert statuses == [409, 409, 429]


async def test_the_endpoints_are_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    delete = schema["paths"]["/api/v1/me"]["delete"]
    assert {"202", "401", "403", "409", "422", "429", "503"} <= set(delete["responses"])
    assert "role_must_be_revoked" in delete["responses"]["409"]["description"]
    assert "account_deletion_pending" in delete["responses"]["403"]["description"]
    restore_op = schema["paths"]["/api/v1/me/restore"]["post"]
    assert {"200", "401", "409", "429"} <= set(restore_op["responses"])
    assert "DeleteAccountRequest" in schema["components"]["schemas"]

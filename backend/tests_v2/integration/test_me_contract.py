"""`MeUser` целиком в `GET /me` и в ответах входа; отсутствие профиля это ошибка, а не молчаливые значения."""

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import (
    LOGIN,
    ME,
    PASSWORD,
    SignedInUser,
    bearer,
    do_refresh,
    execute,
    fetch_all,
    new_credentials,
    register,
    verification_token,
    verified_user,
)

ME_KEYS = {
    "id",
    "username",
    "email",
    "email_verified",
    "role",
    "status",
    "created_at",
    "profile",
    "privacy",
    "counters",
    "required_actions",
}
PROFILE_KEYS = {
    "display_name",
    "avatar",
    "bio",
    "links",
    "birth_date",
    "birth_date_visibility",
    "city",
    "language",
    "timezone",
    "is_private",
    "hidden_fields",
}
PRIVACY_KEYS = {
    "dm_policy",
    "comment_policy",
    "mention_policy",
    "friends_list_visibility",
    "followers_list_visibility",
    "presence_visibility",
    "default_post_visibility",
}


def assert_full_me(user: dict[str, Any]) -> None:
    assert set(user) == ME_KEYS
    assert set(user["profile"]) == PROFILE_KEYS
    assert set(user["privacy"]) == PRIVACY_KEYS
    assert set(user["counters"]) == {
        "unread_notifications",
        "unread_conversations",
        "pending_friend_requests",
        "pending_follow_requests",
    }
    assert user["required_actions"] == []


async def test_verify_email_login_and_refresh_all_return_the_full_user(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    _, body = await register(client, display_name="Иван", language="ru", timezone="Europe/Moscow")
    token = verification_token(jobs, body["email"])
    verify = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert verify.status_code == 200
    signed_in = SignedInUser(credentials=body, auth=verify.json(), response=verify)

    login = await client.post(LOGIN, json={"login": body["email"], "password": PASSWORD})
    refreshed = await do_refresh(client, signed_in.refresh_token)

    expected = (await client.get(ME, headers=signed_in.headers)).json()
    for response in (verify, login, refreshed):
        assert response.status_code == 200, response.text
        user = response.json()["user"]
        assert_full_me(user)
        assert user["profile"]["display_name"] == "Иван"
        assert user["profile"]["language"] == "ru"
        assert user["profile"]["timezone"] == "Europe/Moscow"
        assert user == expected


async def test_a_race_window_refresh_also_returns_the_full_user(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    first = await do_refresh(client, user.refresh_token)
    assert first.status_code == 200

    second = await do_refresh(client, user.refresh_token)  # старый токен в окне гонки вкладок

    assert second.status_code == 200
    assert "set-cookie" not in second.headers
    assert_full_me(second.json()["user"])


async def test_the_user_in_auth_responses_follows_profile_changes(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await client.patch(
        "/api/v1/me/profile",
        json={"display_name": "Новое имя", "is_private": True},
        headers=user.headers,
    )
    await client.patch("/api/v1/me/privacy", json={"dm_policy": "nobody"}, headers=user.headers)

    login = await client.post(
        "/api/v1/auth/login", json={"login": user.credentials["username"], "password": PASSWORD}
    )

    profile = login.json()["user"]["profile"]
    assert (profile["display_name"], profile["is_private"]) == ("Новое имя", True)
    assert login.json()["user"]["privacy"]["dm_policy"] == "nobody"


# ----------------------------------------------------------------------------- нет профиля
async def test_an_account_without_profile_rows_is_an_error_not_silent_defaults(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await execute(admin_engine, "DELETE FROM profile.profiles")

    me = await client.get(ME, headers=user.headers)
    view = await client.get(f"/api/v1/users/{user.user_id}", headers=user.headers)

    assert me.status_code == 500
    assert me.json()["code"] == "internal_error"
    assert view.status_code == 500
    assert "profile rows" not in me.text  # внутреннее сообщение наружу не уходит


async def test_a_login_without_profile_rows_does_not_leave_a_session_behind(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    sessions_before = len(await fetch_all(admin_engine, "SELECT 1 FROM identity.sessions"))
    await execute(admin_engine, "DELETE FROM profile.privacy_settings")

    response = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
    )

    assert response.status_code == 500
    assert "set-cookie" not in response.headers
    sessions_after = len(await fetch_all(admin_engine, "SELECT 1 FROM identity.sessions"))
    assert sessions_after == sessions_before  # сессия, которой клиент не получит, не создаётся


async def test_registration_always_creates_both_rows_even_for_the_same_address(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    first, body = await register(client)
    again = await client.post("/api/v1/auth/register", json=new_credentials(email=body["email"]))

    assert (first.status_code, again.status_code) == (201, 201)
    profiles = await fetch_all(admin_engine, "SELECT user_id FROM profile.profiles")
    privacy = await fetch_all(admin_engine, "SELECT user_id FROM profile.privacy_settings")
    users = await fetch_all(admin_engine, "SELECT id FROM identity.users")
    assert [r["user_id"] for r in profiles] == [r["user_id"] for r in privacy]
    assert [r["user_id"] for r in profiles] == [r["id"] for r in users]


async def test_profile_endpoints_work_with_an_access_token_only(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """Токен из ответа входа годится для всех ручек профиля без дополнительных шагов."""
    user = await verified_user(client, jobs)
    headers = bearer(user.auth["access_token"])

    assert (await client.get(ME, headers=headers)).status_code == 200
    assert (await client.get("/api/v1/me/privacy", headers=headers)).status_code == 200
    assert (await client.patch("/api/v1/me/profile", json={}, headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/users/{user.user_id}", headers=headers)).status_code == 200

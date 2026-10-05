"""S3-03: `GET /users/{ref}`: профиль человека глазами зрителя (UserProfile, политики 4.6)."""

import re
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .helpers import (
    bearer,
    execute,
    fill_profile,
    limited_client,
    register,
    set_privacy,
    two_users,
    url,
    verified_user,
    view,
)

USER_KEYS = {
    "user",
    "bio",
    "links",
    "birth_date",
    "city",
    "language",
    "timezone",
    "is_private",
    "created_at",
    "counters",
    "relationship",
    "presence",
}
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ----------------------------------------------------------------------------- открытый профиль
async def test_a_viewer_sees_an_open_profile_by_username_and_by_id(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs)

    by_name = await view(client, viewer, owner.credentials["username"])
    by_id = await view(client, viewer, owner.user_id)

    assert by_name.status_code == 200, by_name.text
    assert by_name.headers["cache-control"] == "no-store"
    assert by_id.json() == by_name.json()
    body = by_name.json()
    assert set(body) == USER_KEYS
    assert body["user"] == {
        "id": owner.user_id,
        "username": owner.credentials["username"],
        "display_name": "Анна",
        "avatar": None,
    }
    assert body["bio"] == "Люблю горы"
    assert body["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert (body["city"], body["language"], body["timezone"]) == ("Казань", "ru", "Europe/Moscow")
    assert body["birth_date"] == "1990-05-12"
    assert body["is_private"] is False
    assert RFC3339_UTC.fullmatch(body["created_at"])
    assert body["presence"] is None
    assert body["relationship"] == {
        "is_self": False,
        "friendship": "none",
        "friend_request_id": None,
        "following": "none",
        "follows_you": False,
        "blocked": False,
    }


async def test_the_ref_ignores_case_and_accepts_an_uppercase_uuid(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs)
    expected = (await view(client, viewer, owner.user_id)).json()

    assert (await view(client, viewer, owner.credentials["username"].upper())).json() == expected
    assert (await view(client, viewer, owner.user_id.upper())).json() == expected


async def test_the_profile_never_exposes_account_secrets(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs)

    text = (await view(client, viewer, owner.user_id)).text

    assert owner.credentials["email"] not in text
    for word in ("email", "password", "role", "status", "last_login", "session"):
        assert word not in text


@pytest.mark.parametrize(
    ("visibility", "expected"),
    [("hidden", None), ("day_month", "05-12"), ("full", "1990-05-12")],
)
async def test_birth_date_is_shown_in_the_volume_the_owner_chose(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, visibility: str, expected: str | None
) -> None:
    owner, viewer = await two_users(client, jobs, birth_date_visibility=visibility)

    assert (await view(client, viewer, owner.user_id)).json()["birth_date"] == expected


async def test_a_missing_birth_date_is_null_whatever_the_visibility(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs, birth_date=None, birth_date_visibility="full")

    assert (await view(client, viewer, owner.user_id)).json()["birth_date"] is None


async def test_a_day_month_date_keeps_leading_zeros(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(
        client, jobs, birth_date="2000-02-03", birth_date_visibility="day_month"
    )

    assert (await view(client, viewer, owner.user_id)).json()["birth_date"] == "02-03"


# ----------------------------------------------------------------------------- закрытый профиль
async def test_a_private_profile_shows_strangers_only_the_card(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """4.6: у закрытого профиля чужие видят имя, ник, аватар, био и счётчики, остальное скрыто."""
    owner, viewer = await two_users(client, jobs, is_private=True)
    await set_privacy(
        client, owner, friends_list_visibility="everyone", followers_list_visibility="everyone"
    )

    body = (await view(client, viewer, owner.user_id)).json()

    assert body["is_private"] is True
    assert body["user"]["display_name"] == "Анна"
    assert body["bio"] == "Люблю горы"
    assert body["links"] == []
    assert body["birth_date"] is None
    assert (body["city"], body["language"], body["timezone"]) == (None, None, None)
    # Счётчики друзей и подписчиков видны (настройка владельца разрешает), счётчик постов скрыт.
    assert body["counters"] == {"posts": None, "friends": 0, "followers": 0, "following": 0}


async def test_opening_the_profile_again_shows_everything_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs, is_private=True)
    hidden = (await view(client, viewer, owner.user_id)).json()
    assert hidden["city"] is None

    await fill_profile(client, owner, is_private=False)

    shown = (await view(client, viewer, owner.user_id)).json()
    assert (shown["city"], shown["birth_date"]) == ("Казань", "1990-05-12")
    assert shown["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert shown["counters"]["posts"] == 0


# ----------------------------------------------------------------------------- счётчики
@pytest.mark.parametrize(
    ("setting", "friends", "followers", "following"),
    [
        ("everyone", 0, 0, 0),
        ("friends", None, None, None),  # посторонний не друг
        ("only_me", None, None, None),
    ],
)
async def test_counters_follow_the_owners_list_settings(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    setting: str,
    friends: int | None,
    followers: int | None,
    following: int | None,
) -> None:
    owner, viewer = await two_users(client, jobs)
    await set_privacy(
        client, owner, friends_list_visibility=setting, followers_list_visibility=setting
    )

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]

    assert counters == {
        "posts": 0,
        "friends": friends,
        "followers": followers,
        "following": following,
    }


async def test_each_counter_has_its_own_setting_and_following_shares_the_followers_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs)
    await set_privacy(
        client, owner, friends_list_visibility="everyone", followers_list_visibility="only_me"
    )

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]

    assert counters == {"posts": 0, "friends": 0, "followers": None, "following": None}


async def test_the_default_settings_hide_the_social_counters_from_strangers(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer = await two_users(client, jobs)

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]

    assert counters == {"posts": 0, "friends": None, "followers": None, "following": None}


# ----------------------------------------------------------------------------- собственный профиль
async def test_the_owner_sees_everything_of_their_own_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(client, jobs)
    await fill_profile(client, owner, is_private=True, birth_date_visibility="hidden")
    await set_privacy(
        client, owner, friends_list_visibility="only_me", followers_list_visibility="only_me"
    )

    body = (await view(client, owner, owner.credentials["username"])).json()

    assert body["relationship"]["is_self"] is True
    assert body["birth_date"] == "1990-05-12"  # владельцу целиком, даже если скрыта от других
    assert body["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert (body["city"], body["language"], body["timezone"]) == ("Казань", "ru", "Europe/Moscow")
    assert body["counters"] == {"posts": 0, "friends": 0, "followers": 0, "following": 0}


# ----------------------------------------------------------------------------- 404
async def test_an_unknown_user_is_404(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    viewer = await verified_user(client, jobs)

    for ref in ("nobody_here", str(uuid.uuid4())):
        response = await view(client, viewer, ref)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "ref",
    ["ab", "a" * 31, "bad-name", "ivan petrov", "иван", "%00", "0192b7a05c1e7c3a9d543f1a2b6c7d80"],
)
async def test_a_ref_that_is_neither_an_id_nor_a_username_is_404_not_422(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, ref: str
) -> None:
    viewer = await verified_user(client, jobs)

    response = await view(client, viewer, ref)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("status", ["suspended", "banned", "deletion_pending"])
async def test_an_account_that_is_not_active_has_no_profile(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    status: str,
) -> None:
    owner, viewer = await two_users(client, jobs)
    assert (await view(client, viewer, owner.user_id)).status_code == 200

    await execute(
        admin_engine,
        "UPDATE identity.users SET status = :s WHERE username = :u",
        s=status,
        u=owner.credentials["username"],
    )

    assert (await view(client, viewer, owner.user_id)).status_code == 404
    assert (await view(client, viewer, owner.credentials["username"])).status_code == 404


async def test_an_unverified_account_has_no_profile_yet(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    viewer = await verified_user(client, jobs)
    _, pending = await register(client, display_name="Ещё не подтвердил")

    response = await view(client, viewer, pending["username"])

    assert response.status_code == 404


async def test_a_hidden_profile_and_a_missing_one_are_indistinguishable(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, viewer = await two_users(client, jobs)
    await execute(admin_engine, "UPDATE identity.users SET status = 'banned'")

    hidden = await view(client, viewer, owner.credentials["username"])
    missing = await view(client, viewer, "no_such_person")

    assert hidden.status_code == missing.status_code == 404

    def shape(response: httpx.Response) -> dict[str, Any]:
        body: dict[str, Any] = response.json()
        return {key: body[key] for key in ("type", "title", "status", "code", "detail")}

    assert shape(hidden) == shape(missing)


# ----------------------------------------------------------------------------- доступ и лимиты
async def test_the_profile_requires_a_token(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, _ = await two_users(client, jobs)

    anonymous = await client.get(url(owner.user_id))
    forged = await client.get(url(owner.user_id), headers=bearer("not.a.token"))

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "token_missing")
    assert (forged.status_code, forged.json()["code"]) == (401, "token_invalid")


async def test_profile_views_share_the_read_limit(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, api_read=3) as (_, http):
        owner, viewer = await two_users(http, jobs)

        statuses = [(await view(http, viewer, owner.user_id)).status_code for _ in range(5)]

        assert statuses[:3] == [200, 200, 200]
        assert set(statuses[3:]) == {429}


async def test_the_endpoint_is_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    operation = schema["paths"]["/api/v1/users/{ref}"]["get"]
    assert {"200", "401", "404", "429"} <= set(operation["responses"])
    example = schema["components"]["schemas"]["UserProfile"]["examples"][0]
    assert example["user"]["username"] == "anna"
    assert set(example) == USER_KEYS

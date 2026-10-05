"""S3-02: `GET /me/privacy` и `PATCH /me/privacy` (настройки приватности, 4.5, 5.3)."""

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, SignedInUser, fetch_one, verified_user

PRIVACY = "/api/v1/me/privacy"

DEFAULTS = {
    "dm_policy": "friends",
    "comment_policy": "everyone",
    "mention_policy": "everyone",
    "friends_list_visibility": "friends",
    "followers_list_visibility": "friends",
    "presence_visibility": "friends",
    "default_post_visibility": "friends",
}


async def patch(client: httpx.AsyncClient, user: SignedInUser, **body: Any) -> httpx.Response:
    return await client.patch(PRIVACY, json=body, headers=user.headers)


async def test_new_accounts_start_with_the_default_settings(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.get(PRIVACY, headers=user.headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == DEFAULTS


async def test_a_subset_of_settings_can_be_changed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, dm_policy="everyone", presence_visibility="nobody")

    assert response.status_code == 200
    assert response.json() == {**DEFAULTS, "dm_policy": "everyone", "presence_visibility": "nobody"}
    assert (await client.get(PRIVACY, headers=user.headers)).json() == response.json()


async def test_every_setting_accepts_every_value_of_its_enumeration(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    options = {
        "dm_policy": ["everyone", "friends", "nobody"],
        "comment_policy": ["everyone", "friends", "nobody"],
        "mention_policy": ["everyone", "friends", "nobody"],
        "friends_list_visibility": ["everyone", "friends", "only_me"],
        "followers_list_visibility": ["everyone", "friends", "only_me"],
        "presence_visibility": ["everyone", "friends", "nobody"],
        "default_post_visibility": ["public", "friends", "private"],
    }

    for field, values in options.items():
        for value in values:
            response = await patch(client, user, **{field: value})
            assert response.status_code == 200, (field, value, response.text)
            assert response.json()[field] == value


async def test_the_new_settings_show_up_in_me(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    changed = await patch(client, user, comment_policy="nobody", default_post_visibility="private")

    me = (await client.get(ME, headers=user.headers)).json()
    assert me["privacy"] == changed.json()


async def test_an_empty_body_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await patch(client, user, dm_policy="nobody")

    response = await patch(client, user)

    assert response.status_code == 200
    assert response.json() == {**DEFAULTS, "dm_policy": "nobody"}


async def test_settings_belong_to_their_owner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice = await verified_user(client, jobs)
    bob = await verified_user(client, jobs)

    await patch(client, alice, dm_policy="nobody")

    assert (await client.get(PRIVACY, headers=bob.headers)).json() == DEFAULTS


async def test_the_change_is_stored_and_moves_updated_at(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    before = await fetch_one(admin_engine, "SELECT * FROM profile.privacy_settings")

    await patch(client, user, mention_policy="friends")

    after = await fetch_one(admin_engine, "SELECT * FROM profile.privacy_settings")
    assert after["mention_policy"] == "friends"
    assert after["updated_at"] > before["updated_at"]


@pytest.mark.parametrize(
    ("body", "pointer", "code"),
    [
        ({"dm_policy": "only_me"}, "/body/dm_policy", "invalid_enum"),
        ({"friends_list_visibility": "nobody"}, "/body/friends_list_visibility", "invalid_enum"),
        ({"default_post_visibility": "everyone"}, "/body/default_post_visibility", "invalid_enum"),
        ({"dm_policy": ""}, "/body/dm_policy", "invalid_enum"),
        ({"dm_policy": None}, "/body/dm_policy", "invalid_format"),
        ({"dm_policy": 1}, "/body/dm_policy", "invalid_enum"),
        ({"is_private": True}, "/body/is_private", "unknown_field"),
        ({"avatar": "x"}, "/body/avatar", "unknown_field"),
    ],
)
async def test_invalid_settings_give_422_with_pointers(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    body: dict[str, Any],
    pointer: str,
    code: str,
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, **body)

    assert response.status_code == 422
    assert [(e["pointer"], e["code"]) for e in response.json()["errors"]] == [(pointer, code)]
    assert (await client.get(PRIVACY, headers=user.headers)).json() == DEFAULTS


async def test_a_rejected_request_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, dm_policy="everyone", comment_policy="sometimes")

    assert response.status_code == 422
    assert (await client.get(PRIVACY, headers=user.headers)).json() == DEFAULTS


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_both_endpoints_need_a_token(client: httpx.AsyncClient, method: str) -> None:
    response = await client.request(method, PRIVACY, json={} if method == "PATCH" else None)

    assert response.status_code == 401
    assert response.json()["code"] == "token_missing"


async def test_the_endpoints_are_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    path = schema["paths"]["/api/v1/me/privacy"]
    assert {"get", "patch"} <= set(path)
    assert {"200", "401", "429"} <= set(path["get"]["responses"])
    assert "422" in path["patch"]["responses"]
    assert schema["components"]["schemas"]["PrivacySettings"]["examples"][0] == DEFAULTS

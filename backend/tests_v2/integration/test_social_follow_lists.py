"""Списки подписок (S8-01, 5.4, 5.3): свои, чужие с отношением зрителя, доступ, постраничность.

Свои списки (`/me/following`, `/me/followers`) плоские; чужие (`/users/{ref}/followers`, `/following`)
несут `relationship` зрителя к каждому человеку и подчиняются настройке владельца
`followers_list_visibility` и закрытости профиля. Это те же правила, что у списка друзей (S7).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.deps import AppResources
from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import SignedInUser, execute, fill_profile, set_privacy, url, view
from .social_helpers import (
    FOLLOW_REQUESTS,
    MY_FOLLOWERS,
    MY_FOLLOWING,
    SUMMARY_KEYS,
    befriend,
    block,
    code_of,
    follow,
    follow_privately,
    follow_rows,
    follower_ids,
    following_ids,
    forged_cursor,
    renamed_key,
    set_private,
    set_status,
    user_follow_list,
    user_uuid,
    users,
)

LISTS = ("followers", "following")


async def walk(
    client: httpx.AsyncClient, viewer: SignedInUser, address: str, *, limit: int = 2
) -> list[dict[str, Any]]:
    """Все страницы списка по `limit` записей: проверяет заодно, что страницы не повторяются."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(address, params=params, headers=viewer.headers)
        assert response.status_code == 200, response.text
        page = response.json()
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            ids = [item["id"] for item in found]
            assert len(ids) == len(set(ids)), "человек повторился на страницах"
            return found


# ----------------------------------------------------------------------------- свои списки
async def test_my_following_and_my_followers_are_newest_first_page_by_page(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    me, *others = await users(client, jobs, 6)
    for other in others:
        await follow(client, me, other)  # я подписываюсь на пятерых
        await follow(client, other, me)  # и они на меня

    for address in (MY_FOLLOWING, MY_FOLLOWERS):
        first = (await client.get(address, params={"limit": 2}, headers=me.headers)).json()
        assert [item["id"] for item in first["items"]] == [o.user_id for o in others[::-1][:2]]
        assert first["next_cursor"] is not None
        for item in first["items"]:
            assert set(item) == SUMMARY_KEYS  # плоская карточка, без `relationship`
        walked = await walk(client, me, address)
        assert [item["id"] for item in walked] == [other.user_id for other in others[::-1]]
    # Один и тот же человек в обоих списках, но порядок задаёт время своей подписки.
    assert await following_ids(client, me) == await follower_ids(client, me)


async def test_the_lists_of_a_person_are_not_mixed_up(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await follow(client, alice, bob)
    await follow(client, carol, alice)

    assert await following_ids(client, alice) == [bob.user_id]
    assert await follower_ids(client, alice) == [carol.user_id]
    assert await following_ids(client, bob) == []
    assert await follower_ids(client, bob) == [alice.user_id]
    assert await following_ids(client, carol) == [alice.user_id]


async def test_equal_dates_of_follows_neither_lose_nor_repeat_people(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Ключ страницы `(время, человек)`: при одинаковом времени порядок задаёт идентификатор."""
    me, *others = await users(client, jobs, 7)
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for other in others[:3]:
        await execute(
            admin_engine,
            "INSERT INTO social.follows (follower_id, followee_id, created_at) "
            "VALUES (:follower, :followee, :moment)",
            follower=user_uuid(me),
            followee=user_uuid(other),
            moment=moment,
        )
    for other in others[3:]:
        await execute(
            admin_engine,
            "INSERT INTO social.follows (follower_id, followee_id, created_at) "
            "VALUES (:follower, :followee, :moment)",
            follower=user_uuid(other),
            followee=user_uuid(me),
            moment=moment,
        )

    following = [item["id"] for item in await walk(client, me, MY_FOLLOWING)]
    followers = [item["id"] for item in await walk(client, me, MY_FOLLOWERS)]

    assert following == sorted((o.user_id for o in others[:3]), reverse=True)
    assert followers == sorted((o.user_id for o in others[3:]), reverse=True)


async def test_a_page_boundary_in_the_middle_of_equal_dates_loses_nobody(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Страница обрывается посреди людей с одним временем, а за ними идут записи с другим."""
    owner, *fans = await users(client, jobs, 8)
    early = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    late = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    for index, fan in enumerate(fans):
        await execute(
            admin_engine,
            "INSERT INTO social.follows (follower_id, followee_id, created_at) "
            "VALUES (:follower, :followee, :moment)",
            follower=user_uuid(fan),
            followee=user_uuid(owner),
            moment=late if index < 3 else early,
        )

    walked = [item["id"] for item in await walk(client, owner, MY_FOLLOWERS, limit=2)]

    expected = sorted((f.user_id for f in fans[:3]), reverse=True) + sorted(
        (f.user_id for f in fans[3:]), reverse=True
    )
    assert walked == expected


# ----------------------------------------------------------------------------- подделанные курсоры
FORGERIES = [
    # Разбираются и работают как обычный ключ страницы: подделка не ломает список.
    ("naive-time", {"v": 1, "key": "2026-01-01T00:00:00", "id": "{id}"}, 200),
    ("offset-time", {"v": 1, "key": "2026-01-01T00:00:00+05:00", "id": "{id}"}, 200),
    ("far-future", {"v": 1, "key": "9999-12-31T23:59:59.999999+00:00", "id": "{id}"}, 200),
    ("far-past", {"v": 1, "key": "0001-01-01T00:00:00+00:00", "id": "{id}"}, 200),
    ("epoch-number", {"v": 1, "key": 5, "id": "{id}"}, 200),
    ("extra-field", {"v": 1, "key": "2026-01-01T00:00:00+00:00", "id": "{id}", "x": 1}, 200),
    # Время, которое драйвер не переведёт в UTC (год 0 и год 10000), или не курсор вовсе:
    # `400 invalid_cursor`, а не 500 (находка ревью S8).
    ("edge-time-east", {"v": 1, "key": "0001-01-01T00:00:00+01:00", "id": "{id}"}, 400),
    ("edge-time-west", {"v": 1, "key": "9999-12-31T23:59:59-01:00", "id": "{id}"}, 400),
    ("list", [1, 2], 400),
    ("null", None, 400),
    ("not-json", "not json at all", 400),
    ("bad-id", {"v": 1, "key": "2026-01-01T00:00:00+00:00", "id": "x"}, 400),
    ("bad-version", {"v": 2, "key": "2026-01-01T00:00:00+00:00", "id": "{id}"}, 400),
    ("missing-id", {"v": 1, "key": "2026-01-01T00:00:00+00:00"}, 400),
]


@pytest.mark.parametrize(("kind", "shape", "expected"), FORGERIES, ids=[f[0] for f in FORGERIES])
async def test_a_forged_cursor_never_breaks_a_follow_list(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, kind: str, shape: Any, expected: int
) -> None:
    (alice,) = await users(client, jobs, 1)
    fields = {
        MY_FOLLOWING: "since",
        MY_FOLLOWERS: "since",
        FOLLOW_REQUESTS: "created_at",
        f"{url(alice.user_id)}/followers": "since",
        f"{url(alice.user_id)}/following": "since",
    }
    for address, field_name in fields.items():
        payload = (
            renamed_key(cast(dict[str, Any], shape), field_name)
            if isinstance(shape, dict)
            else shape
        )
        response = await client.get(
            address, params={"cursor": forged_cursor(payload)}, headers=alice.headers
        )
        assert response.status_code == expected, (kind, address, response.text)


async def test_the_list_parameters_are_validated(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    (alice,) = await users(client, jobs, 1)
    addresses = [MY_FOLLOWING, MY_FOLLOWERS, FOLLOW_REQUESTS]
    addresses += [f"{url(alice.user_id)}/{kind}" for kind in LISTS]

    for address in addresses:
        for limit in (0, 101, "many"):
            bad = await client.get(address, params={"limit": limit}, headers=alice.headers)
            assert code_of(bad) == (422, "validation_error"), (address, limit)
        broken = await client.get(address, params={"cursor": "abc"}, headers=alice.headers)
        assert code_of(broken) == (400, "invalid_cursor"), address


# ----------------------------------------------------------------------------- кого списки не показывают
async def test_the_lists_hide_people_who_are_not_active(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    me, kept_follower, gone_follower, kept_followee, gone_followee = await users(client, jobs, 5)
    await follow(client, kept_follower, me)
    await follow(client, gone_follower, me)
    await follow(client, me, kept_followee)
    await follow(client, me, gone_followee)
    await set_status(admin_engine, gone_follower, "deletion_pending")
    await set_status(admin_engine, gone_followee, "suspended")

    assert await follower_ids(client, me) == [kept_follower.user_id]
    assert await following_ids(client, me) == [kept_followee.user_id]
    counters = (await view(client, me, me.user_id)).json()["counters"]
    assert (counters["followers"], counters["following"]) == (1, 1)  # счётчик совпадает со списком
    await set_status(admin_engine, gone_follower, "active")  # вернулся: снова на месте
    assert sorted(await follower_ids(client, me)) == sorted(
        [kept_follower.user_id, gone_follower.user_id]
    )


async def test_the_lists_hide_a_blocked_person_even_if_a_stale_follow_remains(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Команды блокировки снимают подписки, но списки не должны полагаться только на это."""
    me, ok, blocked_by_me, blocked_me = await users(client, jobs, 4)
    for other in (ok, blocked_by_me, blocked_me):
        await follow(client, other, me)
        await follow(client, me, other)
    for blocker, blocked in ((me, blocked_by_me), (blocked_me, me)):
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            a=user_uuid(blocker),
            b=user_uuid(blocked),
        )

    assert await follower_ids(client, me) == [ok.user_id]
    assert await following_ids(client, me) == [ok.user_id]
    # В чужом списке зритель не видит тех, кого заблокировал сам или кто заблокировал его.
    owner, viewer, shown, hidden, hiding = await users(client, jobs, 5)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for person in (shown, hidden, hiding):
        await follow(client, person, owner)
        await follow(client, owner, person)
    for blocker, blocked in ((viewer, hidden), (hiding, viewer)):
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            a=user_uuid(blocker),
            b=user_uuid(blocked),
        )
    for kind in LISTS:
        listed = (await user_follow_list(client, viewer, owner.user_id, kind)).json()["items"]
        assert [item["id"] for item in listed] == [shown.user_id], kind


async def test_the_counters_of_a_profile_agree_with_its_lists(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, viewer, *others = await users(client, jobs, 8)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for other in others[:3]:
        await follow(client, other, owner)
    for other in others[3:]:
        await follow(client, owner, other)
    await set_status(admin_engine, others[0], "banned")
    await set_status(admin_engine, others[3], "banned")

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]
    followers = await walk(client, viewer, f"{url(owner.user_id)}/followers")
    following = await walk(client, viewer, f"{url(owner.user_id)}/following")

    assert counters["followers"] == len(followers) == 2
    assert counters["following"] == len(following) == 2


# ----------------------------------------------------------------------------- чужие списки и relationship
async def test_the_followers_of_another_person_carry_my_relationship_to_each(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer, mutual, asked, fan, stranger, friend = await users(client, jobs, 7)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    await set_private(client, asked, True)
    for person in (viewer, mutual, asked, fan, stranger, friend):
        assert (await follow(client, person, owner)).status_code == 200
    await follow(client, viewer, mutual)  # зритель подписан на этого человека
    await follow(client, viewer, asked)  # у закрытого профиля ждёт запрос зрителя
    await follow(client, fan, viewer)  # этот человек подписан на зрителя
    await befriend(client, viewer, friend)  # с этим зритель дружит

    response = await user_follow_list(client, viewer, owner.user_id, "followers", limit=100)

    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {
        viewer.user_id,
        mutual.user_id,
        asked.user_id,
        fan.user_id,
        stranger.user_id,
        friend.user_id,
    }
    assert set(items[mutual.user_id]) == SUMMARY_KEYS | {"relationship"}
    seen = {
        key: (
            item["relationship"]["is_self"],
            item["relationship"]["friendship"],
            item["relationship"]["following"],
            item["relationship"]["follows_you"],
        )
        for key, item in items.items()
    }
    assert seen == {
        viewer.user_id: (True, "none", "none", False),  # сам зритель: с собой связей нет
        mutual.user_id: (False, "none", "following", False),
        asked.user_id: (False, "none", "requested", False),
        fan.user_id: (False, "none", "none", True),
        stranger.user_id: (False, "none", "none", False),
        friend.user_id: (False, "friends", "none", False),
    }
    assert all(item["relationship"]["blocked"] is False for item in items.values())
    assert all(item["relationship"]["friend_request_id"] is None for item in items.values())


async def test_a_page_of_a_persons_followers_costs_the_same_queries_whatever_its_size(
    client: httpx.AsyncClient, app: FastAPI, jobs: InMemoryJobQueue
) -> None:
    """Отношение к каждому человеку собирается на всю страницу сразу, а не по запросу на человека."""
    owner, viewer, *fans = await users(client, jobs, 14)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for fan in fans:
        await follow(client, fan, owner)
        await follow(client, viewer, fan)
    statements: list[str] = []

    def count(
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: Any,
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    resources = cast(AppResources, app.state.resources)  # pyright: ignore[reportUnknownMemberType]
    engine = resources.engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        small = await user_follow_list(client, viewer, owner.user_id, "followers", limit=2)
        for_small = len(statements)
        statements.clear()
        large = await user_follow_list(client, viewer, owner.user_id, "followers", limit=12)
        for_large = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert (len(small.json()["items"]), len(large.json()["items"])) == (2, 12)
    assert for_small == for_large, (for_small, for_large)  # от размера страницы число не зависит


async def test_the_following_of_another_person_lists_whom_that_person_follows(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer, first, second = await users(client, jobs, 4)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    await follow(client, owner, first)
    await follow(client, owner, second)
    await follow(client, viewer, second)

    response = await user_follow_list(client, viewer, owner.user_id, "following", limit=100)

    assert response.status_code == 200, response.text
    items = {item["id"]: item["relationship"] for item in response.json()["items"]}
    assert set(items) == {first.user_id, second.user_id}
    assert items[first.user_id]["following"] == "none"
    assert items[second.user_id]["following"] == "following"  # зритель тоже подписан


async def test_a_list_by_username_pages_like_a_list_by_identifier(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer, *fans = await users(client, jobs, 5)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for fan in fans:
        await follow(client, fan, owner)
    ref = owner.credentials["username"]

    first = (await user_follow_list(client, viewer, ref, "followers", limit=2)).json()
    second = (
        await user_follow_list(
            client, viewer, ref, "followers", limit=2, cursor=first["next_cursor"]
        )
    ).json()

    assert (len(first["items"]), len(second["items"])) == (2, 1)
    assert second["next_cursor"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == {f.user_id for f in fans}


# ----------------------------------------------------------------------------- доступ к спискам
async def test_a_hidden_owner_gives_not_found_and_hidden_people_stay_hidden(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, viewer, shown, away = await users(client, jobs, 4)
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for person in (shown, away):
        await follow(client, person, owner)
    await set_status(admin_engine, away, "suspended")

    listed = (await user_follow_list(client, viewer, owner.user_id, "followers")).json()["items"]

    assert [item["id"] for item in listed] == [shown.user_id]  # неактивного в чужом списке нет
    for kind in LISTS:
        missing = await user_follow_list(client, viewer, str(uuid.uuid4()), kind)
        assert code_of(missing) == (404, "not_found")
    assert (await block(client, owner, viewer)).status_code == 204
    for kind in LISTS:
        assert code_of(await user_follow_list(client, viewer, owner.user_id, kind)) == (
            404,
            "not_found",
        )
    await set_status(admin_engine, owner, "suspended")
    assert code_of(await user_follow_list(client, shown, owner.user_id, "followers")) == (
        404,
        "not_found",
    )


async def test_the_list_of_a_person_follows_his_setting_and_the_privacy_of_his_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, friend, stranger = await users(client, jobs, 3)
    await befriend(client, owner, friend)
    await follow(client, stranger, owner)

    # По умолчанию списки видят только друзья.
    assert (await user_follow_list(client, friend, owner.user_id, "followers")).status_code == 200
    assert code_of(await user_follow_list(client, stranger, owner.user_id, "following")) == (
        403,
        "list_hidden",
    )
    # Свой список виден всегда, даже при `only_me`.
    await set_privacy(client, owner, followers_list_visibility="only_me")
    assert (await user_follow_list(client, owner, owner.user_id, "followers")).status_code == 200
    assert code_of(await user_follow_list(client, friend, owner.user_id, "followers")) == (
        403,
        "list_hidden",
    )
    # `everyone` открывает списки любому, пока профиль открыт.
    await set_privacy(client, owner, followers_list_visibility="everyone")
    assert (await user_follow_list(client, stranger, owner.user_id, "following")).status_code == 200
    # Закрытый профиль закрывает списки от чужих, что бы ни говорила настройка.
    await fill_profile(client, owner, is_private=True)
    other = (await users(client, jobs, 1))[0]
    assert code_of(await user_follow_list(client, other, owner.user_id, "followers")) == (
        403,
        "profile_private",
    )
    assert (await user_follow_list(client, friend, owner.user_id, "followers")).status_code == 200


async def test_the_friends_setting_does_not_open_the_follow_lists(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """Подписчики и подписки живут под `followers_list_visibility`; `friends_list_visibility` им чужая."""
    owner, stranger = await users(client, jobs, 2)
    await set_privacy(client, owner, friends_list_visibility="everyone")  # открыты только друзья
    for kind in LISTS:
        assert code_of(await user_follow_list(client, stranger, owner.user_id, kind)) == (
            403,
            "list_hidden",
        )
    await set_privacy(
        client, owner, friends_list_visibility="only_me", followers_list_visibility="everyone"
    )
    for kind in LISTS:
        assert (await user_follow_list(client, stranger, owner.user_id, kind)).status_code == 200


def expected_follow_list(kind: str, *, is_private: bool, visibility: str) -> tuple[int, str | None]:
    """Матрица доступа к спискам подписчиков и подписок (4.6) в виде таблицы, а не цепочки `if`."""
    if kind in {"blocked_by_viewer", "blocked_by_owner", "owner_not_active"}:
        return 404, "not_found"
    if kind == "self":
        return 200, None
    details_shown = kind in {"friend", "follower"} or not is_private
    if not details_shown:
        return 403, "profile_private"
    shown_by_setting = visibility == "everyone" or (visibility == "friends" and kind == "friend")
    return (200, None) if shown_by_setting else (403, "list_hidden")


async def test_the_access_matrix_of_the_follow_lists_holds_at_the_api_level(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Все виды зрителя против закрытости профиля и настройки списка, через настоящие ручки."""
    owner, friend, follower, stranger, blocked_by_viewer, blocked_by_owner = await users(
        client, jobs, 6
    )
    await set_private(client, owner, True)
    await follow_privately(client, follower, owner)  # подписчик закрытого профиля, одобренный
    await befriend(client, owner, friend)
    assert (await block(client, blocked_by_viewer, owner)).status_code == 204
    assert (await block(client, owner, blocked_by_owner)).status_code == 204
    viewers = {
        "self": owner,
        "friend": friend,
        "follower": follower,
        "stranger": stranger,
        "blocked_by_viewer": blocked_by_viewer,
        "blocked_by_owner": blocked_by_owner,
    }
    assert len(await follow_rows(admin_engine)) == 1

    for is_private in (False, True):
        for visibility in ("everyone", "friends", "only_me"):
            await set_private(client, owner, is_private)
            await set_privacy(client, owner, followers_list_visibility=visibility)
            for kind, viewer in viewers.items():
                for listing in LISTS:
                    response = await user_follow_list(client, viewer, owner.user_id, listing)
                    expected = expected_follow_list(
                        kind, is_private=is_private, visibility=visibility
                    )
                    got = (
                        response.status_code,
                        None if response.is_success else response.json()["code"],
                    )
                    assert got == expected, (kind, listing, is_private, visibility, response.text)

    await set_status(admin_engine, owner, "suspended")
    for kind in ("friend", "follower", "stranger"):
        for listing in LISTS:
            response = await user_follow_list(client, viewers[kind], owner.user_id, listing)
            assert code_of(response) == (404, "not_found"), (kind, listing)

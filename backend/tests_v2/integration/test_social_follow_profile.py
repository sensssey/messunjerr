"""Закрытость профиля и подписки (S8-02, S8-03, S8-06): переключение `is_private`, автоодобрение,
счётчики и `relationship` в профиле, детали закрытого профиля у подписчика.

Раньше (S3) подписчик в профиле был двойником порта, теперь отношение настоящее.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.social.infra.repositories import GraphRepository

from .helpers import ME, fill_profile, set_privacy, view
from .social_helpers import (
    approve_follow,
    assert_graph_is_consistent,
    befriend,
    follow,
    follow_privately,
    follow_request_ids,
    follow_request_rows,
    follow_rows,
    follower_ids,
    following_ids,
    graph_events,
    incoming_follow_requests,
    send_request,
    set_private,
    set_status,
    unfollow,
    users,
)


def event_types(rows: list[dict[str, Any]]) -> list[str]:
    return [row["event_type"] for row in rows]


async def counters_seen_by(
    client: httpx.AsyncClient, viewer: Any, owner: Any
) -> dict[str, int | None]:
    counters: dict[str, int | None] = (await view(client, viewer, owner.user_id)).json()["counters"]
    return counters


# ----------------------------------------------------------------------------- закрыли профиль
async def test_closing_a_profile_keeps_its_followers_and_new_followers_must_ask(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, old_fan, new_fan = await users(client, jobs, 3)
    assert (await follow(client, old_fan, owner)).json() == {"status": "following"}

    await set_private(client, owner, True)

    assert await follower_ids(client, owner) == [old_fan.user_id]  # прежние подписчики остались
    assert (await follow(client, new_fan, owner)).json() == {"status": "requested"}
    assert (await follow(client, old_fan, owner)).json() == {
        "status": "following"
    }  # и идемпотентны
    seen = (await view(client, old_fan, owner.user_id)).json()["relationship"]
    assert seen["following"] == "following"
    assert event_types(await graph_events(admin_engine)) == ["FollowCreated", "FollowRequested"]
    await assert_graph_is_consistent(admin_engine)


async def test_reclosing_does_not_remove_anybody_who_was_approved_by_opening(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)

    await set_private(client, owner, False)
    await set_private(client, owner, True)

    assert await follower_ids(client, owner) == [asker.user_id]
    assert await incoming_follow_requests(client, owner) == []
    assert len(await follow_rows(admin_engine)) == 1


# ----------------------------------------------------------------------------- открыли профиль
async def test_opening_a_profile_approves_every_waiting_request_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, *askers = await users(client, jobs, 5)
    await set_private(client, owner, True)
    for asker in askers:
        await follow(client, asker, owner)
    assert len(await incoming_follow_requests(client, owner)) == 4

    await set_private(client, owner, False)

    assert sorted(await follower_ids(client, owner)) == sorted(a.user_id for a in askers)
    for asker in askers:
        assert await following_ids(client, asker) == [owner.user_id]
        seen = (await view(client, asker, owner.user_id)).json()["relationship"]
        assert seen["following"] == "following"
    assert await incoming_follow_requests(client, owner) == []
    header = (await client.get(ME, headers=owner.headers)).json()["counters"]
    assert header["pending_follow_requests"] == 0
    assert {row["status"] for row in await follow_request_rows(admin_engine)} == {"approved"}
    assert len(await follow_rows(admin_engine)) == 4
    await assert_graph_is_consistent(admin_engine)


async def test_the_automatic_approvals_are_events_of_the_owner_in_the_order_of_followers(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, *askers = await users(client, jobs, 4)
    await set_private(client, owner, True)
    for asker in askers:
        await follow(client, asker, owner)
    requested = len(await graph_events(admin_engine))

    await set_private(client, owner, False)

    approvals = (await graph_events(admin_engine))[requested:]
    assert event_types(approvals) == ["FollowRequestResponded"] * 3
    # По возрастанию идентификатора просившего: так замки пар берутся в одном порядке у всех.
    assert [event["payload"]["follower_id"] for event in approvals] == sorted(
        a.user_id for a in askers
    )
    for event in approvals:
        assert event["payload"]["decision"] == "approved"
        assert event["payload"]["followee_id"] == owner.user_id
        assert event["headers"]["actor_id"] == owner.user_id  # одобрил владелец, открыв профиль
        low, high = sorted([event["payload"]["follower_id"], owner.user_id])
        assert event["key"] == f"{low}:{high}"
    assert "FollowCreated" not in event_types(await graph_events(admin_engine))
    # Отдельный `FollowRequested` у каждого остался, а id запроса в одобрении тот же.
    asked = {
        e["payload"]["follower_id"]: e["payload"]["request_id"]
        for e in (await graph_events(admin_engine))[:requested]
    }
    assert {e["payload"]["follower_id"]: e["payload"]["request_id"] for e in approvals} == asked


async def test_opening_approves_the_requests_of_people_who_are_not_active_too(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Ждущий запрос на открытом профиле навсегда застрял бы в `requested`: его одобряют, подписка
    скрыта от глаз, как и сам человек, и вернётся вместе с ним."""
    owner, active_asker, gone_asker = await users(client, jobs, 3)
    await set_private(client, owner, True)
    await follow(client, active_asker, owner)
    await follow(client, gone_asker, owner)
    await set_status(admin_engine, gone_asker, "suspended")

    await set_private(client, owner, False)

    assert await follower_ids(client, owner) == [active_asker.user_id]  # неактивного не видно
    assert len(await follow_rows(admin_engine)) == 2
    assert {row["status"] for row in await follow_request_rows(admin_engine)} == {"approved"}
    await set_status(admin_engine, gone_asker, "active")
    assert sorted(await follower_ids(client, owner)) == sorted(
        [active_asker.user_id, gone_asker.user_id]
    )
    await assert_graph_is_consistent(admin_engine)


async def test_after_opening_a_follow_is_immediate_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).json() == {"status": "requested"}
    await unfollow(client, asker, owner)

    await set_private(client, owner, False)

    assert (await follow(client, asker, owner)).json() == {"status": "following"}


async def test_other_changes_of_the_profile_approve_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)

    # Правка других полей, повтор `is_private: true` и переход открытый → закрытый не открывают ничего.
    await fill_profile(client, owner, is_private=True, city="Москва")
    await set_private(client, owner, True)
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["pending"]
    assert len(await incoming_follow_requests(client, owner)) == 1
    other = (await users(client, jobs, 1))[0]
    await set_private(client, other, False)  # профиль и так открыт: повтор тоже ничего не делает
    assert event_types(await graph_events(admin_engine)) == ["FollowRequested"]
    assert await follow_rows(admin_engine) == []


async def test_opening_without_waiting_requests_writes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, fan = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow_privately(client, fan, owner)
    before = len(await graph_events(admin_engine))

    await set_private(client, owner, False)

    assert len(await graph_events(admin_engine)) == before
    assert len(await follow_rows(admin_engine)) == 1


class BoomError(Exception):
    pass


async def test_opening_a_profile_is_all_or_nothing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой на втором одобрении откатывает и первое, и саму смену закрытости профиля."""
    owner, *askers = await users(client, jobs, 4)
    await set_private(client, owner, True)
    for asker in askers:
        await follow(client, asker, owner)
    events_before = await graph_events(admin_engine)
    calls = 0
    original = GraphRepository.add_follow

    async def second_one_fails(self: GraphRepository, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BoomError
        await original(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(GraphRepository, "add_follow", second_one_fails)
        answer = await client.patch(
            "/api/v1/me/profile", json={"is_private": False}, headers=owner.headers
        )

    assert answer.status_code == 500
    assert calls == 2
    assert await follow_rows(admin_engine) == []
    assert {row["status"] for row in await follow_request_rows(admin_engine)} == {"pending"}
    assert await graph_events(admin_engine) == events_before
    profile = (await view(client, owner, owner.user_id)).json()
    assert profile["is_private"] is True  # профиль остался закрытым
    # Повтор без сбоя доводит дело до конца.
    await set_private(client, owner, False)
    assert len(await follow_rows(admin_engine)) == 3
    await assert_graph_is_consistent(admin_engine)


# ----------------------------------------------------------------------------- подписчик закрытого профиля
async def test_a_follower_of_a_private_profile_sees_the_details_and_a_stranger_does_not(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, follower, stranger = await users(client, jobs, 3)
    await fill_profile(client, owner, is_private=True)
    await follow_privately(client, follower, owner)

    as_follower = (await view(client, follower, owner.user_id)).json()
    as_stranger = (await view(client, stranger, owner.user_id)).json()

    assert as_follower["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert (as_follower["city"], as_follower["birth_date"]) == ("Казань", "1990-05-12")
    assert as_follower["counters"]["posts"] == 0
    assert (as_stranger["links"], as_stranger["city"], as_stranger["birth_date"]) == (
        [],
        None,
        None,
    )
    assert as_stranger["counters"]["posts"] is None
    # Имя, ник, аватар и био видят все.
    assert as_stranger["bio"] == as_follower["bio"] == "Люблю горы"


async def test_unfollowing_takes_the_details_of_a_private_profile_away_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, follower = await users(client, jobs, 2)
    await fill_profile(client, owner, is_private=True)
    await follow_privately(client, follower, owner)
    assert (await view(client, follower, owner.user_id)).json()["city"] == "Казань"

    assert (await unfollow(client, follower, owner)).status_code == 204

    assert (await view(client, follower, owner.user_id)).json()["city"] is None


async def test_a_follower_of_an_open_profile_sees_what_everybody_sees(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, follower, stranger = await users(client, jobs, 3)
    await fill_profile(client, owner)
    await follow(client, follower, owner)

    assert (await view(client, follower, owner.user_id)).json()["city"] == "Казань"
    assert (await view(client, stranger, owner.user_id)).json()["city"] == "Казань"


# ----------------------------------------------------------------------------- счётчики
async def test_the_counters_of_followers_and_following_follow_the_setting_of_the_owner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, friend, follower, stranger, followee = await users(client, jobs, 5)
    await befriend(client, owner, friend)
    await follow(client, follower, owner)
    await follow(client, owner, followee)
    both = {"followers": 1, "following": 1}
    none = {"followers": None, "following": None}

    def pick(counters: dict[str, int | None]) -> dict[str, int | None]:
        return {key: counters[key] for key in ("followers", "following")}

    # По умолчанию («друзья»): видят владелец и друзья; подписчик и чужой нет.
    assert pick(await counters_seen_by(client, owner, owner)) == both
    assert pick(await counters_seen_by(client, friend, owner)) == both
    assert pick(await counters_seen_by(client, follower, owner)) == none
    assert pick(await counters_seen_by(client, stranger, owner)) == none
    await set_privacy(client, owner, followers_list_visibility="everyone")
    for viewer in (owner, friend, follower, stranger):
        assert pick(await counters_seen_by(client, viewer, owner)) == both
    await set_privacy(client, owner, followers_list_visibility="only_me")
    assert pick(await counters_seen_by(client, owner, owner)) == both  # своё владелец видит всегда
    for viewer in (friend, follower, stranger):
        assert pick(await counters_seen_by(client, viewer, owner)) == none


async def test_the_counters_follow_the_graph_as_it_changes(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)

    def pick(counters: dict[str, int | None]) -> tuple[int | None, int | None]:
        return counters["followers"], counters["following"]

    assert pick(await counters_seen_by(client, bob, bob)) == (0, 0)
    await follow(client, alice, bob)
    await follow(client, carol, bob)
    await follow(client, bob, carol)
    assert pick(await counters_seen_by(client, bob, bob)) == (2, 1)
    assert pick(await counters_seen_by(client, alice, alice)) == (0, 1)
    await unfollow(client, carol, bob)
    assert pick(await counters_seen_by(client, bob, bob)) == (1, 1)


async def test_the_header_counts_waiting_follow_requests_and_friend_requests_separately(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, first, second, friend_asker = await users(client, jobs, 4)
    await set_private(client, owner, True)

    for asker in (first, second):
        await follow(client, asker, owner)
    assert (await send_request(client, friend_asker, owner)).status_code == 201

    header = (await client.get(ME, headers=owner.headers)).json()["counters"]
    assert (header["pending_follow_requests"], header["pending_friend_requests"]) == (2, 1)
    (request_id, *_) = await follow_request_ids(client, owner)
    await approve_follow(client, owner, request_id)
    header = (await client.get(ME, headers=owner.headers)).json()["counters"]
    assert (header["pending_follow_requests"], header["pending_friend_requests"]) == (1, 1)

"""Подписки на открытый профиль (S8-01, 5.4): подписка, отписка, удаление подписчика, ошибки, события.

Закрытые профили и запросы на подписку лежат в `test_social_follow_requests.py`, списки в
`test_social_follow_lists.py`, гонки и замки в `test_social_follow_races.py` и
`test_social_follow_locks.py`.
"""

import uuid

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .helpers import ME, PASSWORD, execute, limited_client, view
from .social_helpers import (
    FOLLOW_REQUESTS,
    FOLLOWS,
    FRIENDS,
    MY_FOLLOWERS,
    MY_FOLLOWING,
    SUMMARY_KEYS,
    assert_graph_is_consistent,
    befriend,
    block,
    code_of,
    decline_follow,
    follow,
    follow_request_rows,
    follow_rows,
    follower_ids,
    following_ids,
    graph_events,
    remove_follower_of,
    set_private,
    set_status,
    unfollow,
    user_follow_list,
    user_uuid,
    users,
)


def event_types(rows: list[dict[str, object]]) -> list[object]:
    return [row["event_type"] for row in rows]


# ----------------------------------------------------------------------------- подписка на открытый профиль
async def test_following_an_open_profile_is_immediate_and_seen_by_both_sides(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)

    answer = await follow(client, alice, bob)

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"status": "following"}
    assert answer.headers["cache-control"] == "no-store"
    mine = (await client.get(MY_FOLLOWING, headers=alice.headers)).json()
    assert mine["next_cursor"] is None
    (item,) = mine["items"]
    assert set(item) == SUMMARY_KEYS
    assert (item["id"], item["username"]) == (bob.user_id, bob.credentials["username"])
    theirs = (await client.get(MY_FOLLOWERS, headers=bob.headers)).json()
    assert [entry["id"] for entry in theirs["items"]] == [alice.user_id]
    # Подписка односторонняя: Борис на Алису не подписан, у Алисы подписчиков нет.
    assert await following_ids(client, bob) == []
    assert await follower_ids(client, alice) == []
    (stored,) = await follow_rows(admin_engine)
    assert (stored["follower_id"], stored["followee_id"]) == (user_uuid(alice), user_uuid(bob))
    assert await follow_request_rows(admin_engine) == []


async def test_a_repeated_follow_is_idempotent_and_writes_no_second_event(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)

    answers = [await follow(client, alice, bob) for _ in range(3)]

    assert [(a.status_code, a.json()) for a in answers] == [(200, {"status": "following"})] * 3
    assert len(await follow_rows(admin_engine)) == 1
    assert event_types(await graph_events(admin_engine)) == ["FollowCreated"]
    first = (await follow_rows(admin_engine))[0]["created_at"]
    await follow(client, alice, bob)
    assert (await follow_rows(admin_engine))[0][
        "created_at"
    ] == first  # время подписки не сдвигается


async def test_the_relationship_in_a_profile_shows_who_follows_whom(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    assert (await follow(client, alice, bob)).status_code == 200

    from_alice = (await view(client, alice, bob.user_id)).json()["relationship"]
    from_bob = (await view(client, bob, alice.user_id)).json()["relationship"]
    from_carol = (await view(client, carol, bob.user_id)).json()["relationship"]

    assert from_alice == {
        "is_self": False,
        "friendship": "none",
        "friend_request_id": None,
        "following": "following",
        "follows_you": False,
        "blocked": False,
    }
    assert (from_bob["following"], from_bob["follows_you"]) == ("none", True)
    assert (from_carol["following"], from_carol["follows_you"]) == ("none", False)
    # Взаимная подписка: у каждого и «подписан», и «подписан на меня».
    assert (await follow(client, bob, alice)).status_code == 200
    again = (await view(client, alice, bob.user_id)).json()["relationship"]
    assert (again["following"], again["follows_you"]) == ("following", True)


async def test_friendship_and_a_follow_are_independent(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    await befriend(client, alice, bob)

    assert (await follow(client, alice, bob)).status_code == 200

    seen = (await view(client, alice, bob.user_id)).json()["relationship"]
    assert (seen["friendship"], seen["following"]) == ("friends", "following")
    # Конец дружбы подписку не трогает, и наоборот.
    assert (
        await client.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers)
    ).status_code == 204
    assert await following_ids(client, alice) == [bob.user_id]
    await befriend(client, alice, bob)
    assert (await unfollow(client, alice, bob)).status_code == 204
    seen = (await view(client, alice, bob.user_id)).json()["relationship"]
    assert (seen["friendship"], seen["following"]) == ("friends", "none")


# ----------------------------------------------------------------------------- отписка
async def test_unfollowing_removes_the_follow_and_writes_one_event(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await follow(client, alice, bob)

    removed = await unfollow(client, alice, bob)

    assert removed.status_code == 204
    assert removed.content == b""
    assert await following_ids(client, alice) == []
    assert await follower_ids(client, bob) == []
    assert await follow_rows(admin_engine) == []
    event = (await graph_events(admin_engine))[-1]
    low, high = sorted([alice.user_id, bob.user_id])
    assert event["event_type"] == "FollowRemoved"
    assert event["key"] == f"{low}:{high}"
    assert event["payload"] == {"follower_id": alice.user_id, "followee_id": bob.user_id}
    assert event["headers"]["actor_id"] == alice.user_id
    # Подписаться можно снова: новая подписка, новое событие.
    assert (await follow(client, alice, bob)).json() == {"status": "following"}
    assert event_types(await graph_events(admin_engine)) == [
        "FollowCreated",
        "FollowRemoved",
        "FollowCreated",
    ]


async def test_unfollowing_is_idempotent_and_never_reveals_anything(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol, gone, blocker = await users(client, jobs, 5)
    await follow(client, alice, bob)
    assert (await block(client, blocker, alice)).status_code == 204
    await set_status(admin_engine, gone, "deletion_pending")

    again = [(await unfollow(client, alice, bob)).status_code for _ in range(3)]
    other = [
        await unfollow(client, alice, carol),  # не подписан
        await unfollow(client, alice, str(uuid.uuid4())),  # такого человека нет
        await unfollow(client, alice, alice),  # сам на себя
        await unfollow(client, alice, gone),  # аккаунт не `active`
        await unfollow(client, alice, blocker),  # заблокировал меня: для меня его нет
    ]

    assert again == [204, 204, 204]
    assert [answer.status_code for answer in other] == [204] * 5
    assert all(answer.content == b"" for answer in other)
    assert event_types(await graph_events(admin_engine)) == [
        "FollowCreated",
        "UserBlocked",
        "FollowRemoved",
    ]


async def test_a_bad_identifier_is_refused_by_the_unfollow_too(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    (alice,) = await users(client, jobs, 1)

    answer = await client.delete(f"{FOLLOWS}/not-a-uuid", headers=alice.headers)

    assert code_of(answer) == (422, "validation_error")


# ----------------------------------------------------------------------------- удаление подписчика
async def test_the_owner_removes_a_follower_and_the_event_names_the_owner_as_actor(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await follow(client, alice, bob)

    removed = await remove_follower_of(client, bob, alice)

    assert removed.status_code == 204
    assert removed.content == b""
    assert await follower_ids(client, bob) == []
    assert await following_ids(client, alice) == []
    assert await follow_rows(admin_engine) == []
    event = (await graph_events(admin_engine))[-1]
    assert event["event_type"] == "FollowRemoved"
    assert event["payload"] == {"follower_id": alice.user_id, "followee_id": bob.user_id}
    assert event["headers"]["actor_id"] == bob.user_id  # убрал владелец, а не подписчик
    # Удалённый подписчик не заблокирован: на открытый профиль он подписывается снова.
    assert (await follow(client, alice, bob)).json() == {"status": "following"}


async def test_removing_a_follower_is_idempotent_and_touches_nothing_else(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await follow(client, alice, bob)
    await follow(client, bob, carol)  # подписка в обратную сторону: её удаление не касается
    before = len(await graph_events(admin_engine))

    results = [
        await remove_follower_of(client, bob, carol),  # Борис подписан на Карину, а не она на него
        await remove_follower_of(client, bob, str(uuid.uuid4())),
        await remove_follower_of(client, bob, bob),
    ]

    assert [answer.status_code for answer in results] == [204, 204, 204]
    assert len(await graph_events(admin_engine)) == before
    assert await follower_ids(client, bob) == [alice.user_id]
    assert await following_ids(client, bob) == [carol.user_id]
    assert (await remove_follower_of(client, bob, alice)).status_code == 204
    assert (await remove_follower_of(client, bob, alice)).status_code == 204  # повтор
    assert event_types(await graph_events(admin_engine))[before:] == ["FollowRemoved"]


# ----------------------------------------------------------------------------- ошибки
async def test_follow_errors_follow_the_specification(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    (alice,) = await users(client, jobs, 1)

    assert code_of(await follow(client, alice, alice)) == (400, "self_action")
    assert code_of(await follow(client, alice, str(uuid.uuid4()))) == (404, "not_found")
    assert code_of(await follow(client, alice, "not-a-uuid")) == (422, "validation_error")
    assert await follow_rows(admin_engine) == []
    assert await follow_request_rows(admin_engine) == []
    assert await graph_events(admin_engine) == []


@pytest.mark.parametrize("status", ["suspended", "banned", "deletion_pending", "pending"])
@pytest.mark.parametrize("private", [False, True], ids=["open", "private"])
async def test_a_person_who_is_not_active_cannot_be_followed(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    status: str,
    private: bool,
) -> None:
    alice, bob = await users(client, jobs, 2)
    if private:
        await set_private(client, bob, True)
    await set_status(admin_engine, bob, status)

    answer = await follow(client, alice, bob)

    assert code_of(answer) == (404, "not_found")
    assert await follow_rows(admin_engine) == []
    assert await follow_request_rows(admin_engine) == []
    assert await graph_events(admin_engine) == []


async def test_a_block_in_either_direction_hides_the_person_from_a_follow(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    assert (await block(client, alice, bob)).status_code == 204  # Алиса заблокировала Бориса
    assert (await block(client, carol, alice)).status_code == 204  # Карина заблокировала Алису
    missing = await follow(client, alice, str(uuid.uuid4()))

    answers = [
        await follow(client, alice, bob),
        await follow(client, bob, alice),
        await follow(client, alice, carol),
        await follow(client, carol, alice),
    ]

    assert [code_of(answer) for answer in answers] == [(404, "not_found")] * 4
    for answer in answers:  # по ответу блокировку не отличить от несуществующего человека
        assert (answer.json()["title"], answer.json()["detail"]) == (
            missing.json()["title"],
            missing.json()["detail"],
        )
    assert await follow_rows(admin_engine) == []
    assert await follow_request_rows(admin_engine) == []


async def test_follow_endpoints_require_a_token(client: httpx.AsyncClient) -> None:
    someone = uuid.uuid4()

    responses = [
        await client.put(f"{FOLLOWS}/{someone}"),
        await client.delete(f"{FOLLOWS}/{someone}"),
        await client.get(MY_FOLLOWING),
        await client.get(MY_FOLLOWERS),
        await client.delete(f"{MY_FOLLOWERS}/{someone}"),
        await client.get(FOLLOW_REQUESTS),
        await client.post(f"{FOLLOW_REQUESTS}/{someone}/approve"),
        await client.post(f"{FOLLOW_REQUESTS}/{someone}/decline"),
        await client.get(f"/api/v1/users/{someone}/followers"),
        await client.get(f"/api/v1/users/{someone}/following"),
    ]

    assert [code_of(r) for r in responses] == [(401, "token_missing")] * 10


async def test_an_account_waiting_for_deletion_cannot_use_follows_but_can_be_restored(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await follow(client, bob, alice)).status_code == 200
    requested = await client.request(
        "DELETE", ME, json={"password": PASSWORD}, headers=alice.headers
    )
    assert requested.status_code == 202, requested.text
    someone = uuid.uuid4()

    refused = [
        await follow(client, alice, bob),
        await unfollow(client, alice, bob),
        await client.get(MY_FOLLOWING, headers=alice.headers),
        await client.get(MY_FOLLOWERS, headers=alice.headers),
        await remove_follower_of(client, alice, bob),
        await client.get(FOLLOW_REQUESTS, headers=alice.headers),
        await decline_follow(client, alice, str(someone)),
        await user_follow_list(client, alice, bob.user_id, "followers"),
    ]

    assert [code_of(response) for response in refused] == [(403, "account_deletion_pending")] * 8
    # Для других такой человек исчез: подписаться на него нельзя, а в списках его нет.
    assert code_of(await follow(client, bob, alice)) == (404, "not_found")
    assert await following_ids(client, bob) == []
    restored = await client.post(f"{ME}/restore", headers=alice.headers)
    assert restored.status_code == 200, restored.text
    assert await following_ids(client, bob) == [alice.user_id]


# ----------------------------------------------------------------------------- события
async def test_the_events_of_a_follow_carry_ids_only_and_the_pair_as_the_partition_key(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await follow(client, alice, bob)
    await unfollow(client, alice, bob)

    events = await graph_events(admin_engine)

    low, high = sorted([alice.user_id, bob.user_id])
    assert event_types(events) == ["FollowCreated", "FollowRemoved"]
    assert {event["key"] for event in events} == {f"{low}:{high}"}
    for event in events:
        assert event["payload"] == {"follower_id": alice.user_id, "followee_id": bob.user_id}
        assert event["headers"]["actor_id"] == alice.user_id
        assert alice.credentials["email"] not in str(event)  # ни имён, ни почты (⚖️)
        assert alice.credentials["username"] not in str(event)


async def test_events_of_one_pair_in_both_directions_share_one_key(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await follow(client, alice, bob)
    await follow(client, bob, alice)

    events = await graph_events(admin_engine)

    assert len({event["key"] for event in events}) == 1
    assert [event["headers"]["actor_id"] for event in events] == [alice.user_id, bob.user_id]


# ----------------------------------------------------------------------------- лимиты и заголовки
async def test_following_is_limited_per_person(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, follow=2) as (_, http):
        alice, *others = await users(http, jobs, 4)

        statuses = [(await follow(http, alice, other)).status_code for other in others]

        assert statuses == [200, 200, 429]
        limited = await follow(http, alice, others[2])
        assert limited.json()["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) > 0
        # Лимит `follow` действует на подписку; отписка живёт под общим потолком записи.
        assert (await unfollow(http, alice, others[0])).status_code == 204


async def test_unfollowing_and_removing_a_follower_are_limited_as_writes(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, api_write=2) as (_, http):
        alice, bob, carol = await users(http, jobs, 3)

        statuses = [
            (await unfollow(http, alice, bob)).status_code,
            (await remove_follower_of(http, alice, carol)).status_code,
            (await unfollow(http, alice, carol)).status_code,
        ]

        assert statuses == [204, 204, 429]


async def test_answers_without_a_body_still_carry_the_limit_and_cache_headers(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    """Готовый `Response(204)` терял заголовки зависимостей (`RateLimit-*`, `Cache-Control`)."""
    async with limited_client(test_settings, jobs) as (_, http):
        owner, follower, asker, target = await users(http, jobs, 4)
        await set_private(http, owner, True)
        assert (await follow(http, follower, owner)).status_code == 200
        pending = (await http.get(FOLLOW_REQUESTS, headers=owner.headers)).json()["items"]
        (request_id,) = [item["id"] for item in pending]
        answers = {
            "PUT /follows": await follow(http, asker, target),
            "DELETE /follows": await unfollow(http, asker, owner),
            "DELETE /me/followers": await remove_follower_of(http, owner, target),
            "POST decline": await decline_follow(http, owner, request_id),
        }

        for name, answer in answers.items():
            assert answer.headers["cache-control"] == "no-store", name
            assert int(answer.headers["ratelimit-limit"]) > 0, name
            assert "ratelimit-remaining" in answer.headers, name
            if name != "PUT /follows":
                assert answer.status_code == 204, (name, answer.text)
                assert answer.content == b"", name
        assert answers["PUT /follows"].status_code == 200


# ----------------------------------------------------------------------------- целостность
async def test_the_database_forbids_a_second_follow_of_the_same_pair(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Страховка БД на случай, если команда когда-нибудь забудет проверку под замком пары."""
    alice, bob = await users(client, jobs, 2)
    await follow(client, alice, bob)

    with pytest.raises(IntegrityError) as caught:
        await execute(
            admin_engine,
            "INSERT INTO social.follows (follower_id, followee_id) VALUES (:a, :b)",
            a=user_uuid(alice),
            b=user_uuid(bob),
        )

    assert "pk_follows" in str(caught.value)
    await assert_graph_is_consistent(admin_engine)

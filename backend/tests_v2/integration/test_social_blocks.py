"""Блокировки (S7-05, S8, 5.4, 4.6): атомарность, взаимная невидимость, идемпотентность, события, список.

С S8 блокировка в той же транзакции снимает подписки в обе стороны и отменяет ждущие запросы на подписку.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.uow import UnitOfWork
from messunjerr.social.commands.blocks import BlockUser, block_user
from messunjerr.social.commands.friend_requests import RespondToRequest, accept_friend_request
from messunjerr.social.domain import events
from messunjerr.social.infra.repositories import GraphRepository

from .helpers import execute, limited_client, url, verified_user, view
from .social_helpers import (
    BLOCKS,
    FRIEND_REQUESTS,
    FRIENDS,
    MY_BLOCKS,
    SUMMARY_KEYS,
    assert_graph_is_consistent,
    befriend,
    block,
    block_rows,
    blocked_ids,
    code_of,
    follow,
    follow_privately,
    follow_request_rows,
    follow_rows,
    follower_ids,
    following_ids,
    friend_ids,
    friendship_rows,
    graph_events,
    incoming_follow_requests,
    request_rows,
    send_request,
    set_private,
    set_status,
    unblock,
    user_follow_list,
    user_uuid,
    users,
)


class BoomError(Exception):
    pass


def event_types(rows: list[dict[str, Any]]) -> list[str]:
    return [row["event_type"] for row in rows]


# ----------------------------------------------------------------------------- последствия блокировки
async def test_blocking_a_friend_ends_the_friendship_and_writes_both_events(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await befriend(client, alice, bob)

    blocked = await block(client, alice, bob)

    assert blocked.status_code == 204
    assert blocked.content == b""
    assert await friend_ids(client, alice) == []
    assert await friend_ids(client, bob) == []
    assert await friendship_rows(admin_engine) == []
    assert [(row["blocker_id"], row["blocked_id"]) for row in await block_rows(admin_engine)] == [
        (user_uuid(alice), user_uuid(bob))
    ]
    written = await graph_events(admin_engine)
    assert event_types(written) == [
        "FriendRequestSent",
        "FriendRequestResponded",
        "UserBlocked",
        "FriendshipRemoved",
    ]
    low, high = sorted([alice.user_id, bob.user_id])
    user_blocked, removed = written[2], written[3]
    assert user_blocked["payload"] == {"blocker_id": alice.user_id, "blocked_id": bob.user_id}
    assert removed["payload"] == {"user_low_id": low, "user_high_id": high}
    # Совершивший действие это поле конверта (6.4), а не сообщения: он лежит в заголовках строки.
    assert user_blocked["headers"]["actor_id"] == removed["headers"]["actor_id"] == alice.user_id
    # Один ключ у обоих событий: потребитель получит их по порядку.
    assert user_blocked["key"] == removed["key"] == f"{low}:{high}"
    await assert_graph_is_consistent(admin_engine)


@pytest.mark.parametrize(
    "blocker_is_sender", [True, False], ids=["sender_blocks", "receiver_blocks"]
)
async def test_blocking_cancels_the_pending_request_whoever_blocks(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    blocker_is_sender: bool,
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await send_request(client, alice, bob)).status_code == 201
    blocker, other = (alice, bob) if blocker_is_sender else (bob, alice)

    assert (await block(client, blocker, other)).status_code == 204

    (stored,) = await request_rows(admin_engine)
    assert stored["status"] == "cancelled"
    assert stored["responded_at"] is not None
    for person in (alice, bob):
        for direction in ("incoming", "outgoing"):
            page = await client.get(
                FRIEND_REQUESTS, params={"direction": direction}, headers=person.headers
            )
            assert page.json()["items"] == []
    # События: заявка и блокировка; отмена заявки блокировкой отдельного события не пишет.
    assert event_types(await graph_events(admin_engine)) == ["FriendRequestSent", "UserBlocked"]
    assert await friendship_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)


# ----------------------------------------------------------------------------- подписки (S8)
@pytest.mark.parametrize(
    "blocker_is_follower", [True, False], ids=["follower_blocks", "followee_blocks"]
)
async def test_blocking_removes_the_follow_and_writes_an_event_for_it(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    blocker_is_follower: bool,
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await follow(client, alice, bob)).status_code == 200
    blocker, other = (alice, bob) if blocker_is_follower else (bob, alice)

    assert (await block(client, blocker, other)).status_code == 204

    assert await follow_rows(admin_engine) == []
    assert await following_ids(client, alice) == []
    assert await follower_ids(client, bob) == []
    written = await graph_events(admin_engine)
    assert event_types(written) == ["FollowCreated", "UserBlocked", "FollowRemoved"]
    low, high = sorted([alice.user_id, bob.user_id])
    removed = written[2]
    assert removed["payload"] == {"follower_id": alice.user_id, "followee_id": bob.user_id}
    assert removed["headers"]["actor_id"] == blocker.user_id  # снял тот, кто заблокировал
    assert written[1]["key"] == removed["key"] == f"{low}:{high}"
    await assert_graph_is_consistent(admin_engine)


async def test_blocking_removes_follows_in_both_directions_and_leaves_others_alone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    for follower, followee in ((alice, bob), (bob, alice), (carol, alice), (alice, carol)):
        assert (await follow(client, follower, followee)).status_code == 200
    before = len(await graph_events(admin_engine))

    assert (await block(client, alice, bob)).status_code == 204

    remaining = {
        (str(row["follower_id"]), str(row["followee_id"]))
        for row in await follow_rows(admin_engine)
    }
    assert remaining == {(carol.user_id, alice.user_id), (alice.user_id, carol.user_id)}
    written = (await graph_events(admin_engine))[before:]
    assert event_types(written) == ["UserBlocked", "FollowRemoved", "FollowRemoved"]
    # Снятые подписки идут по возрастанию подписчика: события пары всегда в одном порядке.
    assert [event["payload"]["follower_id"] for event in written[1:]] == sorted(
        [alice.user_id, bob.user_id]
    )
    assert {event["headers"]["actor_id"] for event in written} == {alice.user_id}
    # Повтор блокировки ничего не меняет и событий не пишет.
    assert (await block(client, alice, bob)).status_code == 204
    assert len(await graph_events(admin_engine)) == before + 3
    await assert_graph_is_consistent(admin_engine)


@pytest.mark.parametrize("blocker_is_asker", [True, False], ids=["asker_blocks", "owner_blocks"])
async def test_blocking_cancels_a_waiting_follow_request_whoever_blocks(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    blocker_is_asker: bool,
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).json() == {"status": "requested"}
    blocker, other = (asker, owner) if blocker_is_asker else (owner, asker)

    assert (await block(client, blocker, other)).status_code == 204

    (stored,) = await follow_request_rows(admin_engine)
    assert stored["status"] == "cancelled"
    assert stored["responded_at"] is not None
    assert await incoming_follow_requests(client, owner) == []
    header = (await client.get("/api/v1/me", headers=owner.headers)).json()["counters"]
    assert header["pending_follow_requests"] == 0
    # События: запрос и блокировка; отмена запроса блокировкой отдельного события не пишет.
    assert event_types(await graph_events(admin_engine)) == ["FollowRequested", "UserBlocked"]
    await assert_graph_is_consistent(admin_engine)


async def test_blocking_cancels_waiting_requests_in_both_directions(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    for person in (alice, bob):
        await set_private(client, person, True)
    assert (await follow(client, alice, bob)).json() == {"status": "requested"}
    assert (await follow(client, bob, alice)).json() == {"status": "requested"}

    assert (await block(client, alice, bob)).status_code == 204

    assert [row["status"] for row in await follow_request_rows(admin_engine)] == [
        "cancelled",
        "cancelled",
    ]
    for person in (alice, bob):
        assert await incoming_follow_requests(client, person) == []
    await assert_graph_is_consistent(admin_engine)


async def test_unblocking_does_not_bring_back_a_follow_of_a_private_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, follower = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow_privately(client, follower, owner)

    assert (await block(client, owner, follower)).status_code == 204
    assert (await unblock(client, owner, follower)).status_code == 204

    # Подписка не вернулась: профиль закрыт, чтобы вернуться, нужен новый запрос.
    assert await follow_rows(admin_engine) == []
    assert (await follow(client, follower, owner)).json() == {"status": "requested"}
    seen = (await view(client, follower, owner.user_id)).json()["relationship"]
    assert seen["following"] == "requested"


async def test_a_block_hides_the_follow_lists_of_a_person_both_ways(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, alice, bob)).status_code == 204

    for viewer, other in ((alice, bob), (bob, alice)):
        for kind in ("followers", "following"):
            answer = await user_follow_list(client, viewer, other.user_id, kind)
            assert code_of(answer) == (404, "not_found"), (viewer is alice, kind)
        assert code_of(await follow(client, viewer, other)) == (404, "not_found")


async def test_blocked_people_do_not_exist_for_each_other(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await befriend(client, alice, carol)
    await befriend(client, bob, carol)
    assert (await block(client, alice, bob)).status_code == 204

    for viewer, other in ((alice, bob), (bob, alice)):  # блокировка скрывает людей в обе стороны
        assert code_of(await view(client, viewer, other.user_id)) == (404, "not_found")
        assert code_of(await view(client, viewer, other.credentials["username"])) == (
            404,
            "not_found",
        )
        assert code_of(await send_request(client, viewer, other)) == (404, "not_found")
        friends_url = f"{url(other.user_id)}/friends"
        assert code_of(await client.get(friends_url, headers=viewer.headers)) == (404, "not_found")
        mutual_url = f"{url(other.user_id)}/mutual-friends"
        assert code_of(await client.get(mutual_url, headers=viewer.headers)) == (404, "not_found")
    # Остальные видят обоих как обычно, а общий друг видит их в чужих списках только без блокировок.
    assert (await view(client, carol, alice.user_id)).status_code == 200
    assert (await view(client, carol, bob.user_id)).status_code == 200


async def test_the_block_list_is_one_sided(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, alice, bob)).status_code == 204

    mine = (await client.get(MY_BLOCKS, headers=alice.headers)).json()
    theirs = (await client.get(MY_BLOCKS, headers=bob.headers)).json()

    assert theirs == {"items": [], "next_cursor": None}  # заблокированный о блокировке не узнаёт
    assert mine["next_cursor"] is None
    (entry,) = mine["items"]
    assert set(entry) == {"user", "blocked_at"}
    assert set(entry["user"]) == SUMMARY_KEYS
    assert entry["user"]["id"] == bob.user_id


async def test_a_person_who_blocked_you_is_hidden_so_blocking_back_is_not_found(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Взаимной блокировки не бывает: заблокировавшего вас для вас нет, как и любого скрытого."""
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, alice, bob)).status_code == 204

    answer = await block(client, bob, alice)

    assert code_of(answer) == (404, "not_found")
    missing = await block(client, bob, str(uuid.uuid4()))
    assert (answer.json()["title"], answer.json()["detail"]) == (
        missing.json()["title"],
        missing.json()["detail"],
    )  # по ответу не отличить от несуществующего
    assert [(r["blocker_id"], r["blocked_id"]) for r in await block_rows(admin_engine)] == [
        (user_uuid(alice), user_uuid(bob))
    ]
    assert await blocked_ids(client, bob) == []  # и профиля Алисы Борис не видит нигде
    assert code_of(await view(client, bob, alice.user_id)) == (404, "not_found")
    # Алиса сняла блокировку: теперь Борис видит её и может заблокировать сам.
    assert (await unblock(client, alice, bob)).status_code == 204
    assert (await view(client, bob, alice.user_id)).status_code == 200
    assert (await block(client, bob, alice)).status_code == 204
    assert code_of(await block(client, alice, bob)) == (404, "not_found")  # теперь наоборот
    assert await blocked_ids(client, alice) == []
    await assert_graph_is_consistent(admin_engine)


async def test_the_database_forbids_a_second_block_for_the_same_pair(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Страховка БД на случай, если команда когда-нибудь забудет проверку под замком пары."""
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, alice, bob)).status_code == 204

    with pytest.raises(IntegrityError):
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:blocker, :blocked)",
            blocker=user_uuid(bob),
            blocked=user_uuid(alice),
        )


async def test_unblocking_does_not_bring_back_the_friendship_or_the_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await befriend(client, alice, bob)
    assert (await send_request(client, alice, carol)).status_code == 201
    assert (await block(client, alice, bob)).status_code == 204
    assert (await block(client, alice, carol)).status_code == 204

    assert (await unblock(client, alice, bob)).status_code == 204
    assert (await unblock(client, alice, carol)).status_code == 204

    assert await friend_ids(client, alice) == []
    assert await friendship_rows(admin_engine) == []
    assert [row["status"] for row in await request_rows(admin_engine)][-1] == "cancelled"
    assert (await block_rows(admin_engine)) == []
    # Дружить можно заново: уже по новой заявке.
    await befriend(client, bob, alice)
    assert await friend_ids(client, alice) == [bob.user_id]


# ----------------------------------------------------------------------------- идемпотентность и события
async def test_blocking_and_unblocking_are_idempotent_and_write_events_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, stranger = await users(client, jobs, 3)

    assert [(await block(client, alice, bob)).status_code for _ in range(3)] == [204, 204, 204]
    assert len(await block_rows(admin_engine)) == 1
    assert event_types(await graph_events(admin_engine)) == ["UserBlocked"]

    assert [(await unblock(client, alice, bob)).status_code for _ in range(3)] == [204, 204, 204]
    assert await block_rows(admin_engine) == []
    assert event_types(await graph_events(admin_engine)) == ["UserBlocked", "UserUnblocked"]
    unblocked = (await graph_events(admin_engine))[-1]
    assert unblocked["payload"] == {"blocker_id": alice.user_id, "blocked_id": bob.user_id}
    assert unblocked["headers"]["actor_id"] == alice.user_id
    # Снять блокировку с того, кого не блокировали, или с неизвестного идентификатора: тоже 204.
    assert (await unblock(client, alice, stranger)).status_code == 204
    assert (await unblock(client, alice, str(uuid.uuid4()))).status_code == 204
    assert (await unblock(client, alice, alice)).status_code == 204
    assert event_types(await graph_events(admin_engine)) == ["UserBlocked", "UserUnblocked"]


async def test_the_repeated_block_keeps_the_original_time(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    await block(client, alice, bob)
    first = (await client.get(MY_BLOCKS, headers=alice.headers)).json()["items"][0]["blocked_at"]

    await block(client, alice, bob)

    again = (await client.get(MY_BLOCKS, headers=alice.headers)).json()["items"][0]["blocked_at"]
    assert again == first


# ----------------------------------------------------------------------------- ошибки
async def test_blocking_yourself_a_missing_person_or_a_bad_id_is_refused(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    (alice,) = await users(client, jobs, 1)

    assert code_of(await block(client, alice, alice)) == (400, "self_action")
    assert code_of(await block(client, alice, str(uuid.uuid4()))) == (404, "not_found")
    assert code_of(await block(client, alice, "not-a-uuid")) == (422, "validation_error")
    assert await block_rows(admin_engine) == []
    assert await graph_events(admin_engine) == []


@pytest.mark.parametrize("status", ["suspended", "banned", "deletion_pending", "pending"])
async def test_blocking_someone_who_is_not_active_is_not_found(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, status: str
) -> None:
    alice, bob = await users(client, jobs, 2)
    await set_status(admin_engine, bob, status)

    assert code_of(await block(client, alice, bob)) == (404, "not_found")
    assert await block_rows(admin_engine) == []


async def test_my_blocks_keep_people_who_have_left_and_they_can_still_be_unblocked(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await block(client, alice, bob)
    await set_status(admin_engine, bob, "deletion_pending")

    events_before = await graph_events(admin_engine)

    # Свой список скрытием аккаунта не чистится, повтор блокировки по-прежнему `204` и ничего не пишет.
    assert await blocked_ids(client, alice) == [bob.user_id]
    assert (await block(client, alice, bob)).status_code == 204
    assert await graph_events(admin_engine) == events_before
    assert (await unblock(client, alice, bob)).status_code == 204
    assert await blocked_ids(client, alice) == []
    # Новую блокировку ушедшего человека уже не поставить: его для вас нет.
    assert code_of(await block(client, alice, bob)) == (404, "not_found")


async def test_a_request_to_a_person_who_blocked_you_looks_like_a_missing_person(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, bob, alice)).status_code == 204

    missing = await send_request(client, alice, str(uuid.uuid4()))
    hidden = await send_request(client, alice, bob)

    assert (hidden.status_code, hidden.json()["code"]) == (
        missing.status_code,
        missing.json()["code"],
    )
    assert hidden.json()["title"] == missing.json()["title"]  # по ответу блокировку не отличить


# ----------------------------------------------------------------------------- список блокировок
async def test_my_blocks_are_listed_newest_first_page_by_page(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, *others = await users(client, jobs, 6)
    for other in others:
        assert (await block(client, alice, other)).status_code == 204

    first = (await client.get(MY_BLOCKS, params={"limit": 2}, headers=alice.headers)).json()
    assert [item["user"]["id"] for item in first["items"]] == [o.user_id for o in others[::-1][:2]]
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            MY_BLOCKS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=alice.headers
        )
    ).json()
    third = (
        await client.get(
            MY_BLOCKS, params={"limit": 2, "cursor": second["next_cursor"]}, headers=alice.headers
        )
    ).json()

    walked = [item["user"]["id"] for page in (first, second, third) for item in page["items"]]
    assert walked == [other.user_id for other in others[::-1]]
    assert (len(second["items"]), len(third["items"])) == (2, 1)
    assert third["next_cursor"] is None
    stamps = [item["blocked_at"] for page in (first, second, third) for item in page["items"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_the_block_list_validates_its_parameters(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    (alice,) = await users(client, jobs, 1)

    for limit in (0, 101, "many"):
        bad = await client.get(MY_BLOCKS, params={"limit": limit}, headers=alice.headers)
        assert code_of(bad) == (422, "validation_error")
    broken = await client.get(MY_BLOCKS, params={"cursor": "abc"}, headers=alice.headers)
    assert code_of(broken) == (400, "invalid_cursor")


async def test_block_endpoints_require_a_token(client: httpx.AsyncClient) -> None:
    someone = uuid.uuid4()

    responses = [
        await client.get(MY_BLOCKS),
        await client.put(f"{BLOCKS}/{someone}"),
        await client.delete(f"{BLOCKS}/{someone}"),
    ]

    assert [code_of(r) for r in responses] == [(401, "token_missing")] * 3


async def test_blocking_is_limited_as_a_write(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    test_settings: Any,
) -> None:
    async with limited_client(test_settings, jobs, api_write=2) as (_, limited):
        alice = await verified_user(limited, jobs)
        others = [await verified_user(limited, jobs) for _ in range(3)]

        statuses = [(await block(limited, alice, other)).status_code for other in others]

        assert statuses == [204, 204, 429]


async def test_answers_without_a_body_still_carry_the_limit_and_cache_headers(
    test_settings: Any, jobs: InMemoryJobQueue
) -> None:
    """Готовый `Response(204)` терял заголовки зависимостей (`RateLimit-*`, `Cache-Control`)."""
    async with limited_client(test_settings, jobs) as (_, http):
        alice, bob, carol = await users(http, jobs, 3)
        await befriend(http, alice, bob)
        first = (await send_request(http, alice, carol)).json()["id"]
        declined = await http.post(f"{FRIEND_REQUESTS}/{first}/decline", headers=carol.headers)
        second = (await send_request(http, alice, carol)).json()["id"]
        answers = {
            "POST decline": declined,
            "DELETE request": await http.delete(
                f"{FRIEND_REQUESTS}/{second}", headers=alice.headers
            ),
            "PUT /blocks": await http.put(f"{BLOCKS}/{carol.user_id}", headers=alice.headers),
            "DELETE /blocks": await http.delete(f"{BLOCKS}/{carol.user_id}", headers=alice.headers),
            "DELETE /friends": await http.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers),
        }

        for name, answer in answers.items():
            assert answer.status_code == 204, (name, answer.text)
            assert answer.content == b"", name
            assert answer.headers["cache-control"] == "no-store", name
            assert int(answer.headers["ratelimit-limit"]) > 0, name
            assert "ratelimit-remaining" in answer.headers, name


# ----------------------------------------------------------------------------- целостность
async def test_block_is_all_or_nothing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой на последнем шаге откатывает всё: блокировка, дружба и события остаются как были."""
    alice, bob = await users(client, jobs, 2)
    await befriend(client, alice, bob)
    before = await graph_events(admin_engine)

    async def explode(*_: Any, **__: Any) -> int:
        raise BoomError

    with monkeypatch.context() as patched:
        patched.setattr(GraphRepository, "cancel_pending_between", explode)
        with pytest.raises(BoomError):
            async with UnitOfWork(sessionmaker) as uow:
                await block_user(
                    BlockUser(actor_id=user_uuid(alice), target_id=user_uuid(bob)), uow=uow
                )

    assert await block_rows(admin_engine) == []
    assert len(await friendship_rows(admin_engine)) == 1
    assert await graph_events(admin_engine) == before
    assert await friend_ids(client, alice) == [bob.user_id]


@pytest.mark.parametrize(
    "failing_step",
    [
        "remove_friendship",
        "cancel_pending_between",
        "remove_follows_between",
        "cancel_follow_requests_between",
        "events.record",
    ],
)
async def test_block_with_follows_is_all_or_nothing_whichever_step_fails(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """Сбой на любом шаге откатывает всё: блокировка, дружба, подписки, запросы и события остаются прежними."""
    alice, bob = await users(client, jobs, 2)
    await befriend(client, alice, bob)
    await set_private(client, bob, True)
    assert (await follow(client, bob, alice)).status_code == 200  # Борис подписан на Алису
    assert (await follow(client, alice, bob)).json() == {
        "status": "requested"
    }  # Алиса просит Бориса
    before_events = await graph_events(admin_engine)
    before_requests = await follow_request_rows(admin_engine)

    async def explode(*_: Any, **__: Any) -> Any:
        raise BoomError

    def explode_sync(*_: Any, **__: Any) -> None:
        raise BoomError

    with monkeypatch.context() as patched:
        if failing_step == "events.record":
            patched.setattr(events, "record", explode_sync)
        else:
            patched.setattr(GraphRepository, failing_step, explode)
        with pytest.raises(BoomError):
            async with UnitOfWork(sessionmaker) as uow:
                await block_user(
                    BlockUser(actor_id=user_uuid(alice), target_id=user_uuid(bob)), uow=uow
                )

    assert await block_rows(admin_engine) == []
    assert len(await friendship_rows(admin_engine)) == 1
    assert len(await follow_rows(admin_engine)) == 1
    assert await follow_request_rows(admin_engine) == before_requests
    assert [row["status"] for row in before_requests] == ["pending"]
    assert await graph_events(admin_engine) == before_events
    await assert_graph_is_consistent(admin_engine)
    # Ничего не потеряно: настоящая блокировка потом проходит целиком.
    assert (await block(client, alice, bob)).status_code == 204
    assert await follow_rows(admin_engine) == []
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["cancelled"]


async def test_accepting_a_request_is_all_or_nothing(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice, bob = await users(client, jobs, 2)
    sent = (await send_request(client, alice, bob)).json()

    def explode(*_: Any, **__: Any) -> None:
        raise BoomError

    with monkeypatch.context() as patched:
        patched.setattr(events, "record", explode)
        with pytest.raises(BoomError):
            async with UnitOfWork(sessionmaker) as uow:
                await accept_friend_request(
                    RespondToRequest(actor_id=user_uuid(bob), request_id=uuid.UUID(sent["id"])),
                    uow=uow,
                )

    (stored,) = await request_rows(admin_engine)
    assert (stored["status"], stored["responded_at"]) == ("pending", None)
    assert await friendship_rows(admin_engine) == []
    assert event_types(await graph_events(admin_engine)) == ["FriendRequestSent"]
    # Заявка осталась в силе: её можно принять по-настоящему.
    assert (
        await client.post(f"{FRIEND_REQUESTS}/{sent['id']}/accept", headers=bob.headers)
    ).status_code == 200


async def test_blocks_with_one_creation_time_are_listed_without_losses_or_repeats(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, *others = await users(client, jobs, 6)
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for other in others:
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id, created_at) "
            "VALUES (:blocker, :blocked, :moment)",
            blocker=user_uuid(alice),
            blocked=user_uuid(other),
            moment=moment,
        )

    walked: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(MY_BLOCKS, params=params, headers=alice.headers)).json()
        walked.extend(item["user"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert walked == sorted((other.user_id for other in others), reverse=True)

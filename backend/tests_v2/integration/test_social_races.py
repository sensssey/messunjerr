"""Гонки социального графа (S7-06, 4.6): одновременные команды над одной парой людей.

Замок пары (`GraphRepository.lock_pair`) превращает любую гонку в одну из двух последовательностей.
Тесты запускают команды в один миг и проверяют, что исход всегда один из допустимых, а инварианты
графа целы. Одновременных запросов не больше дюжины: пул соединений приложения 10 + 10, и каждый
запрос держит одно соединение.
"""

import asyncio
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, SignedInUser
from .social_helpers import (
    FRIENDS,
    accept,
    assert_graph_is_consistent,
    befriend,
    block,
    cancel,
    decline,
    friend_ids,
    friendship_rows,
    graph_events,
    request_ids,
    request_rows,
    send_request,
    users,
)

PAIRS = 6


async def pairs_of_users(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, count: int = PAIRS
) -> list[tuple[SignedInUser, SignedInUser]]:
    people = await users(client, jobs, 2 * count)
    return list(zip(people[::2], people[1::2], strict=True))


async def events_by_pair(engine: AsyncEngine) -> dict[str, list[str]]:
    """Типы событий графа по порядку для каждой пары (ключ партиции `low:high`)."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in await graph_events(engine):
        grouped[row["key"]].append(row["event_type"])
    return dict(grouped)


def pair_key(first: SignedInUser, second: SignedInUser) -> str:
    low, high = sorted([first.user_id, second.user_id])
    return f"{low}:{high}"


def codes(responses: list[httpx.Response]) -> list[int]:
    return [response.status_code for response in responses]


# ----------------------------------------------------------------------------- заявки друг другу
async def test_requests_sent_to_each_other_at_the_same_moment_make_one_friendship(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)

    responses = await asyncio.gather(
        *(
            send_request(client, sender, receiver)
            for first, second in pairs
            for sender, receiver in ((first, second), (second, first))
        )
    )

    for index, (first, second) in enumerate(pairs):
        mine, theirs = responses[2 * index], responses[2 * index + 1]
        assert sorted(codes([mine, theirs])) == [200, 201]  # одна создала, вторая приняла встречную
        assert mine.json()["id"] == theirs.json()["id"]  # заявка одна
        assert {mine.json()["status"], theirs.json()["status"]} == {"pending", "accepted"}
        assert await friend_ids(client, first) == [second.user_id]
        assert await friend_ids(client, second) == [first.user_id]
    assert len(await friendship_rows(admin_engine)) == len(pairs)
    stored = await request_rows(admin_engine)
    assert [row["status"] for row in stored] == ["accepted"] * len(pairs)
    for first, second in pairs:
        assert (await events_by_pair(admin_engine))[pair_key(first, second)] == [
            "FriendRequestSent",
            "FriendRequestResponded",
        ]
    await assert_graph_is_consistent(admin_engine)


async def test_the_same_request_sent_many_times_at_once_is_created_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)

    responses = await asyncio.gather(*(send_request(client, alice, bob) for _ in range(8)))

    assert sorted(codes(responses)) == [201] + [409] * 7
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {
        "friend_request_exists"
    }
    (stored,) = await request_rows(admin_engine)
    assert stored["status"] == "pending"
    assert [row["event_type"] for row in await graph_events(admin_engine)] == ["FriendRequestSent"]


# ----------------------------------------------------------------------------- ответы на заявку
async def test_a_request_accepted_many_times_at_once_makes_one_friendship(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    request_id = (await send_request(client, alice, bob)).json()["id"]

    responses = await asyncio.gather(*(accept(client, bob, request_id) for _ in range(6)))

    assert sorted(codes(responses)) == [200] + [409] * 5
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {
        "friend_request_not_pending"
    }
    assert len(await friendship_rows(admin_engine)) == 1
    assert [row["event_type"] for row in await graph_events(admin_engine)] == [
        "FriendRequestSent",
        "FriendRequestResponded",
    ]


async def test_accept_against_cancel_has_exactly_one_winner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    sent = [(await send_request(client, s, r)).json()["id"] for s, r in pairs]

    results = await asyncio.gather(
        *(
            call
            for (sender, receiver), request_id in zip(pairs, sent, strict=True)
            for call in (accept(client, receiver, request_id), cancel(client, sender, request_id))
        )
    )

    statuses = {str(row["id"]): row["status"] for row in await request_rows(admin_engine)}
    for index, ((sender, receiver), request_id) in enumerate(zip(pairs, sent, strict=True)):
        accept_code, cancel_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        assert (accept_code, cancel_code) in {(200, 409), (409, 204)}, (accept_code, cancel_code)
        winner_accepted = accept_code == 200
        assert statuses[request_id] == ("accepted" if winner_accepted else "cancelled")
        assert await friend_ids(client, sender) == ([receiver.user_id] if winner_accepted else [])
    await assert_graph_is_consistent(admin_engine)


async def test_accept_against_decline_has_exactly_one_winner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    sent = [(await send_request(client, s, r)).json()["id"] for s, r in pairs]

    results = await asyncio.gather(
        *(
            call
            for (_, receiver), request_id in zip(pairs, sent, strict=True)
            for call in (
                accept(client, receiver, request_id),
                decline(client, receiver, request_id),
            )
        )
    )

    for index, (sender, receiver) in enumerate(pairs):
        accept_code, decline_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        assert (accept_code, decline_code) in {(200, 409), (409, 204)}, (accept_code, decline_code)
        assert await friend_ids(client, sender) == (
            [receiver.user_id] if accept_code == 200 else []
        )
        responded = [
            kind
            for kind in (await events_by_pair(admin_engine))[pair_key(sender, receiver)]
            if kind == "FriendRequestResponded"
        ]
        assert len(responded) == 1  # ответ на заявку записан один раз
    await assert_graph_is_consistent(admin_engine)


# ----------------------------------------------------------------------------- блокировка против остального
async def test_block_against_accept_never_leaves_a_friendship_behind(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    sent = [(await send_request(client, s, r)).json()["id"] for s, r in pairs]

    results = await asyncio.gather(
        *(
            call
            for (sender, receiver), request_id in zip(pairs, sent, strict=True)
            for call in (accept(client, receiver, request_id), block(client, sender, receiver))
        )
    )

    grouped = await events_by_pair(admin_engine)
    statuses = {str(row["id"]): row["status"] for row in await request_rows(admin_engine)}
    for index, ((sender, receiver), request_id) in enumerate(zip(pairs, sent, strict=True)):
        accept_code, block_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert block_code == 204
        assert accept_code in {200, 409}, accept_code
        if accept_code == 200:  # принятие успело первым: блокировка потом разрывает дружбу
            assert statuses[request_id] == "accepted"
            assert grouped[pair_key(sender, receiver)] == [
                "FriendRequestSent",
                "FriendRequestResponded",
                "UserBlocked",
                "FriendshipRemoved",
            ]
        else:  # блокировка первой: заявка отменена, принимать нечего
            assert statuses[request_id] == "cancelled"
            assert grouped[pair_key(sender, receiver)] == ["FriendRequestSent", "UserBlocked"]
        assert await friend_ids(client, sender) == []
    assert await friendship_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)


async def test_block_against_a_new_request_leaves_no_live_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)

    results = await asyncio.gather(
        *(
            call
            for blocker, other in pairs
            for call in (send_request(client, other, blocker), block(client, blocker, other))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (blocker, other) in enumerate(pairs):
        send_code, block_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert block_code == 204
        assert send_code in {201, 404}, send_code
        expected = ["FriendRequestSent", "UserBlocked"] if send_code == 201 else ["UserBlocked"]
        assert grouped[pair_key(blocker, other)] == expected
        for person in (blocker, other):
            assert await request_ids(client, person, "incoming") == []
            assert await request_ids(client, person, "outgoing") == []
    assert {row["status"] for row in await request_rows(admin_engine)} <= {"cancelled"}
    await assert_graph_is_consistent(admin_engine)


async def test_removing_a_friend_against_a_block_writes_the_removal_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    for first, second in pairs:
        await befriend(client, first, second)

    results = await asyncio.gather(
        *(
            call
            for first, second in pairs
            for call in (
                client.delete(f"{FRIENDS}/{second.user_id}", headers=first.headers),
                block(client, second, first),
            )
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (first, second) in enumerate(pairs):
        delete_code, block_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert block_code == 204
        assert delete_code in {204, 404}, delete_code  # 404: блокировка успела первой
        kinds = grouped[pair_key(first, second)]
        assert kinds.count("FriendshipRemoved") == 1, kinds  # дружба кончилась один раз
        assert kinds.count("UserBlocked") == 1
    assert await friendship_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)


# ----------------------------------------------------------------------------- разные пары не мешают друг другу
async def test_many_people_sending_to_one_and_one_accepting_all_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    hub, *senders = await users(client, jobs, 13)

    sent = await asyncio.gather(*(send_request(client, sender, hub) for sender in senders))

    assert set(codes(sent)) == {201}
    counters: dict[str, Any] = (await client.get(ME, headers=hub.headers)).json()["counters"]
    assert counters["pending_friend_requests"] == len(senders)
    assert len(await request_ids(client, hub, "incoming")) == len(senders)

    accepted = await asyncio.gather(
        *(accept(client, hub, response.json()["id"]) for response in sent)
    )

    assert set(codes(accepted)) == {200}
    assert sorted(await friend_ids(client, hub)) == sorted(s.user_id for s in senders)
    counters = (await client.get(ME, headers=hub.headers)).json()["counters"]
    assert counters["pending_friend_requests"] == 0
    await assert_graph_is_consistent(admin_engine)

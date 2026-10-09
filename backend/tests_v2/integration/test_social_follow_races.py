"""Гонки подписок (S8-06, 4.6): одновременные команды над одной парой и против открытия профиля.

Замок пары и чтение строки профиля `FOR SHARE` превращают любую гонку в одну из допустимых
последовательностей. Тесты запускают команды в один миг и проверяют, что исход всегда один из них, а
инварианты графа целы (в том числе «у открытого профиля нет ждущих запросов»). Одновременных запросов
не больше дюжины: пул соединений приложения 10 + 10, и каждый запрос держит одно соединение.
Детерминированные тесты порядка блокировок лежат в `test_social_follow_locks.py`.
"""

import asyncio
import random
import uuid
from collections.abc import Coroutine
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, SignedInUser
from .social_helpers import (
    approve_follow,
    assert_events_explain_follows,
    assert_graph_is_consistent,
    block,
    decline_follow,
    follow,
    follow_request_ids,
    follow_request_rows,
    follow_rows,
    follower_ids,
    following_ids,
    graph_events,
    patch_private,
    remove_follower_of,
    set_private,
    unblock,
    unfollow,
    users,
)
from .test_social_races import codes, events_by_pair, pair_key, pairs_of_users

ROUNDS = 10
FANS = 3


async def request_of(client: httpx.AsyncClient, owner: SignedInUser) -> str:
    (request_id,) = await follow_request_ids(client, owner)
    return request_id


# ----------------------------------------------------------------------------- подписка против подписки
async def test_the_same_follow_made_many_times_at_once_is_created_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)

    responses = await asyncio.gather(*(follow(client, alice, bob) for _ in range(8)))

    assert codes(responses) == [200] * 8
    assert {r.json()["status"] for r in responses} == {"following"}
    assert len(await follow_rows(admin_engine)) == 1
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FollowCreated"]


async def test_the_same_request_made_many_times_at_once_is_created_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await set_private(client, bob, True)

    responses = await asyncio.gather(*(follow(client, alice, bob) for _ in range(8)))

    assert codes(responses) == [200] * 8
    assert {r.json()["status"] for r in responses} == {"requested"}
    (stored,) = await follow_request_rows(admin_engine)
    assert stored["status"] == "pending"
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FollowRequested"]
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_follows_in_both_directions_at_the_same_moment_make_two_follows(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)

    responses = await asyncio.gather(
        *(
            follow(client, who, whom)
            for first, second in pairs
            for who, whom in ((first, second), (second, first))
        )
    )

    assert codes(responses) == [200] * (2 * len(pairs))
    assert len(await follow_rows(admin_engine)) == 2 * len(pairs)
    grouped = await events_by_pair(admin_engine)
    for first, second in pairs:
        assert grouped[pair_key(first, second)] == ["FollowCreated", "FollowCreated"]
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_many_people_following_one_open_profile_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """`FOR SHARE` на строке одного профиля: подписчики друг друга не ждут и не теряются."""
    hub, *fans = await users(client, jobs, 13)

    responses = await asyncio.gather(*(follow(client, fan, hub) for fan in fans))

    assert codes(responses) == [200] * len(fans)
    assert sorted(await follower_ids(client, hub)) == sorted(fan.user_id for fan in fans)
    counters = (await client.get(f"/api/v1/users/{hub.user_id}", headers=hub.headers)).json()
    assert counters["counters"]["followers"] == len(fans)
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_many_people_asking_one_private_profile_and_the_owner_approving_all_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    hub, *askers = await users(client, jobs, 13)
    await set_private(client, hub, True)

    asked = await asyncio.gather(*(follow(client, asker, hub) for asker in askers))

    assert {a.json()["status"] for a in asked} == {"requested"}
    header = (await client.get(ME, headers=hub.headers)).json()["counters"]
    assert header["pending_follow_requests"] == len(askers)
    waiting = await follow_request_ids(client, hub)
    approved = await asyncio.gather(*(approve_follow(client, hub, request) for request in waiting))

    assert set(codes(approved)) == {200}
    assert sorted(await follower_ids(client, hub)) == sorted(a.user_id for a in askers)
    header = (await client.get(ME, headers=hub.headers)).json()["counters"]
    assert header["pending_follow_requests"] == 0
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- подписка против отписки
async def test_a_follow_against_an_unfollow_leaves_a_state_that_the_events_explain(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    for first, second in pairs:
        assert (await follow(client, first, second)).status_code == 200

    results = await asyncio.gather(
        *(
            call
            for first, second in pairs
            for call in (follow(client, first, second), unfollow(client, first, second))
        )
    )

    grouped = await events_by_pair(admin_engine)
    stored = {
        (str(row["follower_id"]), str(row["followee_id"]))
        for row in await follow_rows(admin_engine)
    }
    for index, (first, second) in enumerate(pairs):
        follow_code, unfollow_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        assert (follow_code, unfollow_code) == (200, 204)
        kinds = grouped[pair_key(first, second)]
        # Число созданных минус число снятых равно тому, что осталось в таблице: событиям можно верить.
        remains = (first.user_id, second.user_id) in stored
        assert kinds.count("FollowCreated") - kinds.count("FollowRemoved") == (1 if remains else 0)
        assert set(kinds) <= {"FollowCreated", "FollowRemoved"}
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_removing_a_follower_against_a_follow_of_the_same_person_is_consistent(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    for follower, owner in pairs:
        assert (await follow(client, follower, owner)).status_code == 200

    results = await asyncio.gather(
        *(
            call
            for follower, owner in pairs
            for call in (
                follow(client, follower, owner),
                remove_follower_of(client, owner, follower),
            )
        )
    )

    grouped = await events_by_pair(admin_engine)
    present = {
        (str(r["follower_id"]), str(r["followee_id"])) for r in await follow_rows(admin_engine)
    }
    for index, (follower, owner) in enumerate(pairs):
        assert (results[2 * index].status_code, results[2 * index + 1].status_code) == (200, 204)
        kinds = grouped[pair_key(follower, owner)]
        net = kinds.count("FollowCreated") - kinds.count("FollowRemoved")
        assert net == (1 if (follower.user_id, owner.user_id) in present else 0)
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- ответы на запросы
async def test_a_request_approved_many_times_at_once_makes_one_follow(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    request_id = await request_of(client, owner)

    responses = await asyncio.gather(*(approve_follow(client, owner, request_id) for _ in range(6)))

    assert sorted(codes(responses)) == [200] + [409] * 5
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {
        "follow_request_not_pending"
    }
    assert len(await follow_rows(admin_engine)) == 1
    assert [e["event_type"] for e in await graph_events(admin_engine)] == [
        "FollowRequested",
        "FollowRequestResponded",
    ]


async def test_approve_against_decline_has_exactly_one_winner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)  # во всех парах владелец второй
    requests: list[str] = []
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)
        requests.append(await request_of(client, owner))

    results = await asyncio.gather(
        *(
            call
            for (_, owner), request_id in zip(pairs, requests, strict=True)
            for call in (
                approve_follow(client, owner, request_id),
                decline_follow(client, owner, request_id),
            )
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        approve_code, decline_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        assert (approve_code, decline_code) in {(200, 409), (409, 204)}, (
            approve_code,
            decline_code,
        )
        assert await following_ids(client, asker) == (
            [owner.user_id] if approve_code == 200 else []
        )
        responded = [k for k in grouped[pair_key(asker, owner)] if k == "FollowRequestResponded"]
        assert len(responded) == 1  # ответ на запрос записан один раз
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_approve_against_the_cancel_of_the_asker_is_consistent(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    requests: list[str] = []
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)
        requests.append(await request_of(client, owner))

    results = await asyncio.gather(
        *(
            call
            for (asker, owner), request_id in zip(pairs, requests, strict=True)
            for call in (approve_follow(client, owner, request_id), unfollow(client, asker, owner))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        approve_code, cancel_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        # Одобрение успело первым: отписка снимает подписку. Отмена первой: одобрять нечего.
        assert (approve_code, cancel_code) in {(200, 204), (409, 204)}, (approve_code, cancel_code)
        assert await following_ids(client, asker) == []  # в обоих порядках подписки не остаётся
        kinds = grouped[pair_key(asker, owner)]
        if approve_code == 200:
            assert kinds == ["FollowRequested", "FollowRequestResponded", "FollowRemoved"]
        else:
            assert kinds == ["FollowRequested"]
    assert {row["status"] for row in await follow_request_rows(admin_engine)} <= {
        "approved",
        "cancelled",
    }
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- блокировка против подписок
async def test_block_against_a_follow_never_leaves_a_follow_or_a_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)
    for index, (blocker, _) in enumerate(pairs):
        await set_private(client, blocker, index % 2 == 1)  # половина закрытых профилей

    results = await asyncio.gather(
        *(
            call
            for blocker, other in pairs
            for call in (follow(client, other, blocker), block(client, blocker, other))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (blocker, other) in enumerate(pairs):
        follow_code, block_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert block_code == 204
        assert follow_code in {200, 404}, follow_code
        kinds = grouped[pair_key(blocker, other)]
        if follow_code == 404:  # блокировка успела первой
            assert kinds == ["UserBlocked"]
        elif index % 2 == 1:  # закрытый профиль: запрос, потом блокировка закрыла его без события
            assert kinds == ["FollowRequested", "UserBlocked"]
        else:  # открытый профиль: подписка, потом блокировка сняла её
            assert kinds == ["FollowCreated", "UserBlocked", "FollowRemoved"]
    assert await follow_rows(admin_engine) == []
    assert {row["status"] for row in await follow_request_rows(admin_engine)} <= {"cancelled"}
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_block_against_the_approval_of_a_request_is_consistent(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)  # во всех парах владелец второй
    requests: list[str] = []
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)
        requests.append(await request_of(client, owner))

    results = await asyncio.gather(
        *(
            call
            for (asker, owner), request_id in zip(pairs, requests, strict=True)
            for call in (approve_follow(client, owner, request_id), block(client, owner, asker))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        approve_code, block_code = (
            results[2 * index].status_code,
            results[2 * index + 1].status_code,
        )
        assert block_code == 204
        assert approve_code in {200, 409}, approve_code
        kinds = grouped[pair_key(asker, owner)]
        if approve_code == 200:  # одобрение успело первым: блокировка потом снимает подписку
            assert kinds == [
                "FollowRequested",
                "FollowRequestResponded",
                "UserBlocked",
                "FollowRemoved",
            ]
        else:  # блокировка первой: запрос отменён, одобрять нечего
            assert kinds == ["FollowRequested", "UserBlocked"]
    assert await follow_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- открытие профиля
ROUND_DELAYS = [0.0, 0.002, 0.004, 0.006, 0.01, 0.015, 0.02, 0.03, 0.045, 0.07]


async def delayed_opening(
    client: httpx.AsyncClient, owner: SignedInUser, delay: float
) -> httpx.Response:
    await asyncio.sleep(delay)
    return await patch_private(client, owner, False)


async def test_following_against_opening_the_profile_never_leaves_a_waiting_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """`PUT /follows/{id}` против `PATCH /me/profile {"is_private": false}`, 10 кругов на разных парах.

    Подписка идёт либо до открытия (запрос, который открытие потом одобрит), либо после него (сразу).
    В любом случае у каждого подписчика есть подписка, а у открытого профиля нет ждущих запросов.
    """
    rounds: list[tuple[SignedInUser, list[SignedInUser]]] = []
    for delay in ROUND_DELAYS[:ROUNDS]:
        owner, *fans = await users(client, jobs, 1 + FANS)
        await set_private(client, owner, True)
        opened, *answers = await asyncio.gather(
            delayed_opening(client, owner, delay), *(follow(client, fan, owner) for fan in fans)
        )
        assert opened.status_code == 200, opened.text
        assert codes(answers) == [200] * FANS, [a.text for a in answers]
        assert {a.json()["status"] for a in answers} <= {"following", "requested"}
        assert sorted(await follower_ids(client, owner)) == sorted(f.user_id for f in fans)
        rounds.append((owner, fans))

    grouped = await events_by_pair(admin_engine)
    for owner, fans in rounds:
        for fan in fans:
            kinds = grouped[pair_key(owner, fan)]
            assert kinds in (["FollowCreated"], ["FollowRequested", "FollowRequestResponded"]), (
                kinds
            )
    assert {row["status"] for row in await follow_request_rows(admin_engine)} <= {"approved"}
    assert len(await follow_rows(admin_engine)) == ROUNDS * FANS
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_approving_a_request_against_opening_the_profile_makes_one_follow(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)  # во всех парах владелец второй
    requests: list[str] = []
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)
        requests.append(await request_of(client, owner))

    results = await asyncio.gather(
        *(
            call
            for (_, owner), request_id in zip(pairs, requests, strict=True)
            for call in (
                approve_follow(client, owner, request_id),
                patch_private(client, owner, False),
            )
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        approve_code, open_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert open_code == 200
        assert approve_code in {200, 409}, approve_code  # 409: открытие успело одобрить первым
        assert grouped[pair_key(asker, owner)] == ["FollowRequested", "FollowRequestResponded"]
        assert await following_ids(client, asker) == [owner.user_id]
    assert len(await follow_rows(admin_engine)) == len(pairs)
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_cancelling_a_request_against_opening_the_profile_leaves_no_follow_behind(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)  # во всех парах владелец второй
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)

    results = await asyncio.gather(
        *(
            call
            for asker, owner in pairs
            for call in (unfollow(client, asker, owner), patch_private(client, owner, False))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        cancel_code, open_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert (cancel_code, open_code) == (204, 200)
        kinds = grouped[pair_key(asker, owner)]
        # Отмена первой: подписки не было. Открытие первым: подписка появилась и отмена её сняла.
        assert kinds in (
            ["FollowRequested"],
            ["FollowRequested", "FollowRequestResponded", "FollowRemoved"],
        ), kinds
        assert await following_ids(client, asker) == []
    assert await follow_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_blocking_against_opening_the_profile_leaves_no_follow_behind(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    pairs = await pairs_of_users(client, jobs)  # во всех парах владелец второй
    for asker, owner in pairs:
        await set_private(client, owner, True)
        await follow(client, asker, owner)

    results = await asyncio.gather(
        *(
            call
            for asker, owner in pairs
            for call in (block(client, owner, asker), patch_private(client, owner, False))
        )
    )

    grouped = await events_by_pair(admin_engine)
    for index, (asker, owner) in enumerate(pairs):
        block_code, open_code = results[2 * index].status_code, results[2 * index + 1].status_code
        assert (block_code, open_code) == (204, 200)
        kinds = grouped[pair_key(asker, owner)]
        assert kinds in (
            ["FollowRequested", "UserBlocked"],  # блокировка первой: запрос закрыт
            ["FollowRequested", "FollowRequestResponded", "UserBlocked", "FollowRemoved"],
        ), kinds
    assert await follow_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- взаимная блокировка транзакций
async def test_two_owners_opening_their_profiles_at_once_do_not_deadlock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """У двух владельцев общие просившие и запросы друг к другу: открытие обоих разом не зависает."""
    for _ in range(3):
        first, second, *askers = await users(client, jobs, 5)
        for owner in (first, second):
            await set_private(client, owner, True)
        for asker in askers:
            await follow(client, asker, first)
            await follow(client, asker, second)
        await follow(client, first, second)
        await follow(client, second, first)

        opened = await asyncio.wait_for(
            asyncio.gather(
                patch_private(client, first, False), patch_private(client, second, False)
            ),
            timeout=30,
        )

        assert codes(list(opened)) == [200, 200]
    assert {row["status"] for row in await follow_request_rows(admin_engine)} == {"approved"}
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


async def test_three_owners_with_requests_to_each_other_open_their_profiles_without_deadlock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Кольцо из трёх владельцев: замки пар берутся по возрастанию просившего, цикла ожидания нет.

    Запросы к среднему владельцу вставлены в обратном порядке (сначала от старшего): без сортировки
    его открытие взяло бы замки навстречу остальным и замкнуло бы кольцо.
    """
    for _ in range(3):
        people = await users(client, jobs, 3)
        low, middle, high = sorted(people, key=lambda person: person.user_id)
        for owner in people:
            await set_private(client, owner, True)
        # Порядок вставки задаёт порядок строк без сортировки: у `middle` сначала `high`, потом `low`.
        for asker, owner in (
            (middle, low),
            (high, low),
            (high, middle),
            (low, middle),
            (low, high),
            (middle, high),
        ):
            assert (await follow(client, asker, owner)).json() == {"status": "requested"}

        opened = await asyncio.wait_for(
            asyncio.gather(*(patch_private(client, owner, False) for owner in people)), timeout=30
        )

        assert codes(list(opened)) == [200, 200, 200]
        for owner in people:
            assert len(await follower_ids(client, owner)) == 2
    assert len(await follow_rows(admin_engine)) == 3 * 6
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)


# ----------------------------------------------------------------------------- шторм случайных команд
STORM_ROUNDS = 8
STORM_WIDTH = 12
STORM_KINDS = (
    ["follow"] * 5
    + ["unfollow"] * 2
    + ["remove"] * 2
    + ["approve"] * 2
    + ["decline"]
    + ["open"] * 2
    + ["close"] * 2
    + ["block", "unblock"]
)


async def test_a_storm_of_random_commands_never_fails_and_keeps_the_invariants(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Дюжина случайных команд разом, несколько кругов: ни `5xx`, ни зависания, инварианты и события сходятся.

    Здесь вместе все команды подписок, открытие и закрытие профилей и блокировки на шести людях, из
    которых трое с закрытым профилем: пары пересекаются, а замки и строки профилей берутся в разном порядке
    обращений. Любой цикл ожидания PostgreSQL разорвал бы ошибкой `5xx`, а гонка оставила бы след в таблицах.
    """
    rng = random.Random(2026)
    people = await users(client, jobs, 6)
    for person in people[:3]:
        await set_private(client, person, True)
    by_id = {person.user_id: person for person in people}

    for _ in range(STORM_ROUNDS):
        waiting = [
            (str(row["id"]), str(row["followee_id"]))
            for row in await follow_request_rows(admin_engine)
            if row["status"] == "pending"
        ]
        calls: list[Coroutine[Any, Any, httpx.Response]] = []
        for _ in range(STORM_WIDTH):
            actor, other = rng.sample(people, 2)
            match rng.choice(STORM_KINDS):
                case "follow":
                    calls.append(follow(client, actor, other))
                case "unfollow":
                    calls.append(unfollow(client, actor, other))
                case "remove":
                    calls.append(remove_follower_of(client, actor, other))
                case "approve" | "decline" as kind:
                    request_id, owner_id = (
                        rng.choice(waiting) if waiting else (str(uuid.uuid4()), "")
                    )
                    owner = by_id.get(owner_id, actor)
                    answer = approve_follow if kind == "approve" else decline_follow
                    calls.append(answer(client, owner, request_id))
                case "open":
                    calls.append(patch_private(client, actor, False))
                case "close":
                    calls.append(patch_private(client, actor, True))
                case "block":
                    calls.append(block(client, actor, other))
                case _:
                    calls.append(unblock(client, actor, other))

        answers = await asyncio.wait_for(asyncio.gather(*calls), timeout=90)

        assert {answer.status_code for answer in answers} <= {200, 204, 404, 409}, [
            (answer.status_code, answer.text[:120])
            for answer in answers
            if answer.status_code >= 400
        ]
        await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)

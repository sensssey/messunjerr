"""Замки подписок (S8, 4.6): замок пары и строка профиля владельца, детерминированно.

Гонки в `test_social_follow_races.py` случайны: без замка они могут пройти по счастливому порядку.
Здесь замок держит отдельное соединение, и команда обязана ждать, пока его не отпустят. Убрать
`lock_pair` из любой команды подписок или чтение строки профиля `FOR SHARE` значит покраснеть именно
здесь. Порядок блокировок один для всех: строка профиля владельца, затем замок пары.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import SignedInUser
from .social_helpers import (
    approve_follow,
    assert_graph_is_consistent,
    decline_follow,
    follow,
    follow_request_ids,
    follow_request_rows,
    follow_rows,
    follower_ids,
    graph_events,
    patch_private,
    remove_follower_of,
    set_private,
    unfollow,
    user_uuid,
    users,
)
from .test_social_locks import WAIT, held_pair_locks, pair_lock

Call = Callable[[], Coroutine[Any, Any, httpx.Response]]


@asynccontextmanager
async def profile_row_lock(
    engine: AsyncEngine,
    owner: SignedInUser,
    *,
    mode: str = "UPDATE",
    make_public: bool = False,
    also: Sequence[tuple[str, dict[str, Any]]] = (),
) -> AsyncGenerator[None]:
    """Держит строку профиля владельца в отдельной транзакции; в конце фиксирует её.

    `make_public` открывает профиль внутри этой транзакции, `also` выполняет в ней ещё запросы:
    изменения видны другим только после фиксации, то есть тогда, когда замок отпущен.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text(f"SELECT 1 FROM profile.profiles WHERE user_id = :id FOR {mode}"),
            {"id": user_uuid(owner)},
        )
        if make_public:
            await connection.execute(
                text("UPDATE profile.profiles SET is_private = false WHERE user_id = :id"),
                {"id": user_uuid(owner)},
            )
        for statement, values in also:
            await connection.execute(text(statement), values)
        try:
            yield
        finally:
            await transaction.commit()


# ----------------------------------------------------------------------------- замок пары
# Каждая запись: как подготовить пару (первый подписчик, второй владелец) и что вызвать.
async def _follow_open(client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser) -> Call:
    return lambda: follow(client, asker, owner)


async def _follow_private(
    client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser
) -> Call:
    await set_private(client, owner, True)
    return lambda: follow(client, asker, owner)


async def _follow_again(
    client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser
) -> Call:
    assert (await follow(client, asker, owner)).status_code == 200
    return lambda: follow(client, asker, owner)


async def _unfollow(client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser) -> Call:
    assert (await follow(client, asker, owner)).status_code == 200
    return lambda: unfollow(client, asker, owner)


async def _cancel_request(
    client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser
) -> Call:
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).status_code == 200
    return lambda: unfollow(client, asker, owner)


async def _remove_follower(
    client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser
) -> Call:
    assert (await follow(client, asker, owner)).status_code == 200
    return lambda: remove_follower_of(client, owner, asker)


async def _approve(client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser) -> Call:
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).status_code == 200
    (request_id,) = await follow_request_ids(client, owner)
    return lambda: approve_follow(client, owner, request_id)


async def _decline(client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser) -> Call:
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).status_code == 200
    (request_id,) = await follow_request_ids(client, owner)
    return lambda: decline_follow(client, owner, request_id)


async def _open_the_profile(
    client: httpx.AsyncClient, asker: SignedInUser, owner: SignedInUser
) -> Call:
    await set_private(client, owner, True)
    assert (await follow(client, asker, owner)).status_code == 200
    return lambda: patch_private(client, owner, False)


COMMANDS: dict[str, tuple[Callable[..., Awaitable[Call]], int]] = {
    "follow_open": (_follow_open, 200),
    "follow_private": (_follow_private, 200),
    "follow_again": (_follow_again, 200),
    "unfollow": (_unfollow, 204),
    "cancel_request": (_cancel_request, 204),
    "remove_follower": (_remove_follower, 204),
    "approve": (_approve, 200),
    "decline": (_decline, 204),
    "open_the_profile": (_open_the_profile, 200),
}


@pytest.mark.parametrize("name", list(COMMANDS))
async def test_a_follow_command_waits_for_the_pair_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, name: str
) -> None:
    prepare, expected = COMMANDS[name]
    asker, owner = await users(client, jobs, 2)
    call = await prepare(client, asker, owner)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(call())
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished, f"{name}: команда не стала ждать замок пары"

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.status_code == expected, (name, answer.text)
    await assert_graph_is_consistent(admin_engine)


async def test_a_follow_over_another_pair_does_not_wait_for_the_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Замок на пару, а не на всех: другие люди работают, пока одна пара занята."""
    alice, bob, carol, dave = await users(client, jobs, 4)

    async with pair_lock(admin_engine, alice, bob):
        answer = await asyncio.wait_for(follow(client, carol, dave), timeout=5)

    assert answer.status_code == 200


async def test_the_lock_does_not_depend_on_who_follows_whom(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Один замок на пару в любом порядке людей: встречные подписки ждут одного и того же замка."""
    alice, bob = await users(client, jobs, 2)

    async with pair_lock(admin_engine, bob, alice):
        first = asyncio.create_task(follow(client, alice, bob))
        second = asyncio.create_task(follow(client, bob, alice))
        finished, _ = await asyncio.wait({first, second}, timeout=WAIT)
        assert not finished

    assert [(await asyncio.wait_for(t, timeout=15)).status_code for t in (first, second)] == [
        200,
        200,
    ]


async def test_follow_commands_release_the_lock_with_their_transaction_whatever_the_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """И успех, и отказ (`404` откатывает транзакцию) отпускают замок: после ответа он не висит."""
    alice, bob = await users(client, jobs, 2)
    await set_private(client, bob, True)

    answers = [
        await follow(client, alice, bob),  # запрос
        await follow(client, alice, bob),  # повтор: ничего не меняется, транзакция откатывается
        await follow(client, alice, str(uuid.uuid4())),  # такого человека нет: 404
        await unfollow(client, alice, bob),
        await remove_follower_of(client, bob, alice),
    ]

    assert [answer.status_code for answer in answers] == [200, 200, 404, 204, 204]
    assert await held_pair_locks(admin_engine) == 0
    # Строку профиля тоже никто не держит: открытие профиля не ждёт ни секунды.
    opened = await asyncio.wait_for(patch_private(client, bob, False), timeout=5)
    assert opened.status_code == 200


# ----------------------------------------------------------------------------- строка профиля владельца
async def test_a_follow_reads_the_privacy_under_the_row_lock_and_sees_the_new_value(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Пока другая транзакция меняет профиль, подписка ждёт и после неё видит открытый профиль.

    Без `FOR SHARE` подписка прочла бы старое «закрыт» и оставила запрос на открытом профиле.
    """
    asker, owner = await users(client, jobs, 2)
    await set_private(client, owner, True)

    async with profile_row_lock(admin_engine, owner, make_public=True):
        running = asyncio.create_task(follow(client, asker, owner))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished, "подписка не стала ждать строку профиля"

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.json() == {"status": "following"}  # профиль уже открыт: подписка сразу
    assert await follower_ids(client, owner) == [asker.user_id]
    assert await follow_request_rows(admin_engine) == []
    await assert_graph_is_consistent(admin_engine)


async def test_a_follow_shares_the_profile_row_with_other_follows(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """`FOR SHARE`, а не `FOR UPDATE`: подписки на один профиль друг друга не ждут."""
    owner, first, second = await users(client, jobs, 3)

    async with profile_row_lock(admin_engine, owner, mode="SHARE"):
        answer = await asyncio.wait_for(follow(client, first, owner), timeout=5)
        other = await asyncio.wait_for(follow(client, second, owner), timeout=5)

    assert (answer.status_code, other.status_code) == (200, 200)


async def test_opening_a_profile_waits_for_a_follow_that_is_still_in_flight(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Владелец берёт строку профиля `FOR UPDATE` первым делом: подписка в пути её не обгонит."""
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)

    async with profile_row_lock(admin_engine, owner, mode="SHARE"):  # так выглядит подписка в пути
        running = asyncio.create_task(patch_private(client, owner, False))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished, "открытие профиля не стало ждать строку"

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.status_code == 200
    assert await follower_ids(client, owner) == [asker.user_id]  # и запрос одобрен


async def test_opening_decides_on_the_value_that_is_current_after_waiting_for_the_row(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Открытие читает строку профиля `FOR UPDATE` первым делом и решает по свежему значению.

    Пока другая транзакция закрывает профиль и принимает запрос, открытие ждёт, а потом видит
    «закрыт». Без блокировки в начале команда прочла бы устаревшее «открыт», не заметила бы, что
    открывает закрытый профиль, и не сообщила бы об этом порту: ждущий запрос остался бы на открытом
    профиле (так выглядит двойное нажатие «закрыть» и «открыть» с двух устройств).
    """
    owner, asker = await users(client, jobs, 2)  # профиль открыт

    async with profile_row_lock(
        admin_engine,
        owner,
        also=[
            (
                "UPDATE profile.profiles SET is_private = true WHERE user_id = :owner",
                {"owner": user_uuid(owner)},
            ),
            (
                (
                    "INSERT INTO social.follow_requests (follower_id, followee_id) "
                    "VALUES (:asker, :owner)"
                ),
                {"asker": user_uuid(asker), "owner": user_uuid(owner)},
            ),
        ],
    ):
        running = asyncio.create_task(patch_private(client, owner, False))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished

    assert (await asyncio.wait_for(running, timeout=15)).status_code == 200
    assert await follower_ids(client, owner) == [asker.user_id]  # запрос одобрен открытием
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["approved"]
    await assert_graph_is_consistent(admin_engine)


async def test_a_follow_in_flight_holds_the_profile_row_before_it_waits_for_the_pair(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Порядок «строка профиля, затем замок пары» виден снаружи.

    Подписка стоит на замке своей пары, уже держа строку профиля `FOR SHARE`; открытие профиля
    поэтому ждёт её, а после неё одобряет свежий запрос. Если бы подписка брала сначала замок пары,
    а строку потом, открытие профиля прошло бы мимо неё, и запрос остался бы на открытом профиле.
    """
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)

    async with pair_lock(admin_engine, asker, owner):
        following = asyncio.create_task(follow(client, asker, owner))
        finished, _ = await asyncio.wait({following}, timeout=WAIT)
        assert not finished  # подписка ждёт замок пары
        opening = asyncio.create_task(patch_private(client, owner, False))
        finished, _ = await asyncio.wait({opening}, timeout=WAIT)
        assert not finished, "открытие профиля прошло мимо подписки, которая уже читала профиль"

    answered = await asyncio.wait_for(following, timeout=15)
    opened = await asyncio.wait_for(opening, timeout=15)
    assert answered.json() == {"status": "requested"}  # прочла профиль, пока он был закрыт
    assert opened.status_code == 200
    assert await follower_ids(client, owner) == [asker.user_id]  # и открытие подхватило запрос
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["approved"]
    assert [event["event_type"] for event in await graph_events(admin_engine)] == [
        "FollowRequested",
        "FollowRequestResponded",
    ]
    await assert_graph_is_consistent(admin_engine)


async def test_opening_a_profile_holds_the_row_while_it_waits_for_a_pair_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Открытие профиля, ждущее замок пары, строку не отпускает: чужая подписка идёт после него."""
    owner, waiting, newcomer = await users(client, jobs, 3)
    await set_private(client, owner, True)
    await follow(client, waiting, owner)

    async with pair_lock(admin_engine, waiting, owner):
        opening = asyncio.create_task(patch_private(client, owner, False))
        finished, _ = await asyncio.wait({opening}, timeout=WAIT)
        assert not finished  # стоит на замке пары ждущего запроса
        joining = asyncio.create_task(follow(client, newcomer, owner))
        finished, _ = await asyncio.wait({joining}, timeout=WAIT)
        assert not finished, "подписка прошла, хотя профиль ещё меняется"

    assert (await asyncio.wait_for(opening, timeout=15)).status_code == 200
    answer = await asyncio.wait_for(joining, timeout=15)
    assert answer.json() == {"status": "following"}  # профиль уже открыт: сразу
    assert sorted(await follower_ids(client, owner)) == sorted([waiting.user_id, newcomer.user_id])
    assert len(await follow_rows(admin_engine)) == 2
    await assert_graph_is_consistent(admin_engine)

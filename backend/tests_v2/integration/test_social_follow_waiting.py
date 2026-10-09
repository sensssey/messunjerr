"""Команды подписок читают и пишут только под замком пары (S8, ревью): детерминированно.

`test_social_follow_locks.py` проверяет, что команда ждёт замок. Но команда, которая сначала
прочла или записала, а потом встала на замок, тоже «ждёт», хотя решает по устаревшему состоянию.
Здесь замок держит отдельное соединение, пока команда на нём стоит, SQL меняет строки пары, и после
освобождения команда обязана решить по новому состоянию. Перенос `lock_pair` после чтения или
записи краснит именно эти тесты.
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
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
    code_of,
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
from .test_social_follow_locks import COMMANDS
from .test_social_locks import WAIT, pair_lock

TOUCH_LIMIT = 3.0
"""Сколько секунд чужому SQL ждать строку пары, пока команда стоит на замке (должен пройти сразу)."""


async def change(engine: AsyncEngine, statement: str, **values: Any) -> None:
    """Меняет строки пары отдельной транзакцией. Если ожидающая команда уже держит строку, SQL
    встанет за ней, а тест упадёт по тайм-ауту: команда успела тронуть пару раньше замка."""

    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(text(statement), values)

    try:
        await asyncio.wait_for(run(), timeout=TOUCH_LIMIT)
    except TimeoutError:
        pytest.fail("команда держит строки пары, пока ждёт замок: она тронула их раньше замка")


# ----------------------------------------------------------------------------- ничего не тронуто
@pytest.mark.parametrize("name", list(COMMANDS))
async def test_a_command_waiting_for_the_pair_lock_has_touched_no_row_of_the_pair(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, name: str
) -> None:
    prepare, expected = COMMANDS[name]
    asker, owner = await users(client, jobs, 2)
    call = await prepare(client, asker, owner)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(call())
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished, f"{name}: команда не стала ждать замок пары"
        for table in ("social.follows", "social.follow_requests", "social.blocks"):
            await change(admin_engine, f"UPDATE {table} SET created_at = created_at")

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.status_code == expected, (name, answer.text)
    await assert_graph_is_consistent(admin_engine)


# ----------------------------------------------------------------------------- решение по новому состоянию
async def test_an_approval_decides_on_the_request_as_it_is_after_the_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    asker, owner = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(approve_follow(client, owner, request_id))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished
        await change(
            admin_engine,
            "UPDATE social.follow_requests SET status = 'cancelled', responded_at = now()",
        )

    answer = await asyncio.wait_for(running, timeout=15)
    assert code_of(answer) == (409, "follow_request_not_pending")
    assert await follow_rows(admin_engine) == []  # устаревший «ждёт» не создал подписку
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FollowRequested"]


async def test_a_refusal_decides_on_the_request_as_it_is_after_the_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    asker, owner = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(decline_follow(client, owner, request_id))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished
        await change(
            admin_engine,
            "UPDATE social.follow_requests SET status = 'cancelled', responded_at = now()",
        )

    answer = await asyncio.wait_for(running, timeout=15)
    assert code_of(answer) == (409, "follow_request_not_pending")
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FollowRequested"]


Removal = Callable[
    [httpx.AsyncClient, SignedInUser, SignedInUser], Coroutine[Any, Any, httpx.Response]
]
REMOVALS: dict[str, Removal] = {
    # Отписывается подписчик, подписчика убирает владелец: порядок людей у вызовов разный.
    "unfollow": lambda http, asker, owner: unfollow(http, asker, owner),
    "remove_follower": lambda http, asker, owner: remove_follower_of(http, owner, asker),
}


@pytest.mark.parametrize("name", list(REMOVALS))
async def test_removing_a_follow_that_is_already_gone_after_the_lock_writes_no_event(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, name: str
) -> None:
    asker, owner = await users(client, jobs, 2)
    await follow(client, asker, owner)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(REMOVALS[name](client, asker, owner))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished
        await change(admin_engine, "DELETE FROM social.follows")

    assert (await asyncio.wait_for(running, timeout=15)).status_code == 204
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FollowCreated"]


async def test_a_follow_sees_the_follow_that_appeared_while_it_waited(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    asker, owner = await users(client, jobs, 2)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(follow(client, asker, owner))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished
        await change(
            admin_engine,
            "INSERT INTO social.follows (follower_id, followee_id) VALUES (:a, :o)",
            a=user_uuid(asker),
            o=user_uuid(owner),
        )

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.json() == {"status": "following"}  # не вторая вставка и не `500`
    assert len(await follow_rows(admin_engine)) == 1
    assert await graph_events(admin_engine) == []  # подписку создала не команда: событий нет


async def test_a_follow_sees_the_request_that_appeared_while_it_waited(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    asker, owner = await users(client, jobs, 2)
    await set_private(client, owner, True)

    async with pair_lock(admin_engine, asker, owner):
        running = asyncio.create_task(follow(client, asker, owner))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished
        await change(
            admin_engine,
            "INSERT INTO social.follow_requests (follower_id, followee_id) VALUES (:a, :o)",
            a=user_uuid(asker),
            o=user_uuid(owner),
        )

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.json() == {"status": "requested"}
    assert len(await follow_request_rows(admin_engine)) == 1  # не второй ждущий запрос
    assert await graph_events(admin_engine) == []


async def test_opening_skips_a_request_that_was_cancelled_while_it_waited_for_its_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Открытие профиля читает запрос заново под замком: отменённый за это время не одобряется."""
    owner, one, two = await users(client, jobs, 3)
    await set_private(client, owner, True)
    await follow(client, one, owner)
    await follow(client, two, owner)
    # Идёт по возрастанию подписчика: первым будет тот, чей идентификатор меньше.
    first, second = sorted((one, two), key=lambda user: uuid.UUID(user.user_id))

    async with pair_lock(admin_engine, first, owner):
        opening = asyncio.create_task(patch_private(client, owner, False))
        finished, _ = await asyncio.wait({opening}, timeout=WAIT)
        assert not finished  # стоит на замке пары первого подписчика
        await change(
            admin_engine,
            "UPDATE social.follow_requests SET status = 'cancelled', responded_at = now() "
            "WHERE follower_id = :first",
            first=user_uuid(first),
        )

    assert (await asyncio.wait_for(opening, timeout=15)).status_code == 200
    assert await follower_ids(client, owner) == [second.user_id]  # отменённый не стал подписчиком
    statuses = {
        row["follower_id"]: row["status"] for row in await follow_request_rows(admin_engine)
    }
    assert statuses == {user_uuid(first): "cancelled", user_uuid(second): "approved"}
    responded = [
        e for e in await graph_events(admin_engine) if e["event_type"].endswith("Responded")
    ]
    assert [e["payload"]["follower_id"] for e in responded] == [second.user_id]
    await assert_graph_is_consistent(admin_engine)

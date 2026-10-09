"""Замок пары (S7, 4.6): каждая команда над парой людей ждёт его, а не бежит наперегонки.

Гонки в `test_social_races.py` случайны: без замка они могут пройти по счастливому порядку. Здесь
замок держит отдельное соединение, и команда обязана ждать, пока его не отпустят. Убрать `lock_pair`
из любой команды значит покраснеть именно здесь.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.social.infra.repositories import pair_lock_key

from .helpers import SignedInUser, fetch_one
from .social_helpers import (
    FRIENDS,
    accept,
    befriend,
    block,
    cancel,
    decline,
    send_request,
    unblock,
    user_uuid,
    users,
)

WAIT = 0.7
"""Сколько команда обязана простоять на замке, прежде чем мы его отпустим."""


@asynccontextmanager
async def pair_lock(
    engine: AsyncEngine, first: SignedInUser, second: SignedInUser
) -> AsyncGenerator[None]:
    """Держит замок пары в отдельной транзакции, пока идёт тело `async with`."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": pair_lock_key(user_uuid(first), user_uuid(second))},
        )
        try:
            yield
        finally:
            await transaction.rollback()


Call = Callable[[], Coroutine[Any, Any, httpx.Response]]


async def _sent(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> str:
    return str((await send_request(client, alice, bob)).json()["id"])


# Каждая запись: как подготовить пару, что вызвать и какой ответ ждать после освобождения замка.
async def _send(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    return lambda: send_request(client, alice, bob)


async def _accept(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    request_id = await _sent(client, alice, bob)
    return lambda: accept(client, bob, request_id)


async def _decline(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    request_id = await _sent(client, alice, bob)
    return lambda: decline(client, bob, request_id)


async def _cancel(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    request_id = await _sent(client, alice, bob)
    return lambda: cancel(client, alice, request_id)


async def _remove_friend(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    await befriend(client, alice, bob)
    return lambda: client.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers)


async def _block(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    return lambda: block(client, alice, bob)


async def _block_again(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    assert (await block(client, alice, bob)).status_code == 204
    return lambda: block(client, alice, bob)


async def _unblock(client: httpx.AsyncClient, alice: SignedInUser, bob: SignedInUser) -> Call:
    assert (await block(client, alice, bob)).status_code == 204
    return lambda: unblock(client, alice, bob)


COMMANDS: dict[str, tuple[Callable[..., Awaitable[Call]], int]] = {
    "send": (_send, 201),
    "accept": (_accept, 200),
    "decline": (_decline, 204),
    "cancel": (_cancel, 204),
    "remove_friend": (_remove_friend, 204),
    "block": (_block, 204),
    "block_again": (_block_again, 204),
    "unblock": (_unblock, 204),
}


@pytest.mark.parametrize("name", list(COMMANDS))
async def test_a_command_over_a_pair_waits_for_the_pair_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, name: str
) -> None:
    prepare, expected = COMMANDS[name]
    alice, bob = await users(client, jobs, 2)
    call = await prepare(client, alice, bob)

    async with pair_lock(admin_engine, alice, bob):
        running = asyncio.create_task(call())
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished, f"{name}: команда не стала ждать замок пары"

    answer = await asyncio.wait_for(running, timeout=15)
    assert answer.status_code == expected, (name, answer.text)


async def test_a_command_over_another_pair_does_not_wait_for_the_lock(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Замок на пару, а не на всех: другие люди работают, пока одна пара занята."""
    alice, bob, carol, dave = await users(client, jobs, 4)

    async with pair_lock(admin_engine, alice, bob):
        answer = await asyncio.wait_for(send_request(client, carol, dave), timeout=5)

    assert answer.status_code == 201


async def test_the_lock_does_not_depend_on_who_asks_first(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Один замок на пару в любом порядке людей: встречная заявка ждёт того же замка."""
    alice, bob = await users(client, jobs, 2)

    async with pair_lock(admin_engine, bob, alice):  # порядок наоборот
        running = asyncio.create_task(send_request(client, alice, bob))
        finished, _ = await asyncio.wait({running}, timeout=WAIT)
        assert not finished

    assert (await asyncio.wait_for(running, timeout=15)).status_code == 201


async def held_pair_locks(engine: AsyncEngine) -> int:
    """Сколько рекомендательных замков сейчас держат соединения этой базы."""
    row = await fetch_one(
        engine,
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND database = "
        "(SELECT oid FROM pg_database WHERE datname = current_database())",
    )
    return int(row["n"])


async def test_commands_release_the_lock_with_their_transaction_whatever_the_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """И успех, и отказ (`404` откатывает транзакцию) отпускают замок: после ответа он не висит."""
    alice, bob = await users(client, jobs, 2)
    assert (await block(client, bob, alice)).status_code == 204
    assert await held_pair_locks(admin_engine) == 0

    refused = await send_request(client, alice, bob)  # блокировка: 404, транзакция откатывается
    assert refused.status_code == 404
    assert await held_pair_locks(admin_engine) == 0

    assert (await unblock(client, bob, alice)).status_code == 204
    assert (await asyncio.wait_for(send_request(client, alice, bob), timeout=5)).status_code == 201
    assert await held_pair_locks(admin_engine) == 0

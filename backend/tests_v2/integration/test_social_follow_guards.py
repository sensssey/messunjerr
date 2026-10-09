"""Защита подписок по итогам ревью S8: лимит чтения, заголовки, аккаунт без профиля, затор на замке.

Что здесь проверяется и почему: уберите `limit_user("api_read")` или `no_store` у любого списка
подписок, и красным станет только этот файл; ответ `503` при заторе на строке профиля и значения
запросов, не попадающие в текст ошибки БД, не держит больше ничто.
"""

import asyncio

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.db import create_engine
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .helpers import execute, limited_client
from .social_helpers import (
    API,
    assert_graph_is_consistent,
    code_of,
    follow,
    follow_rows,
    graph_events,
    user_uuid,
    users,
)
from .test_social_follow_locks import profile_row_lock
from .test_social_locks import held_pair_locks

READ_ADDRESSES = [
    "/me/following",
    "/me/followers",
    "/me/follow-requests",
    "/users/{me}/followers",
    "/users/{me}/following",
]


@pytest.mark.parametrize("address", READ_ADDRESSES)
async def test_a_follow_list_is_limited_as_a_read_and_is_never_cached(
    test_settings: Settings, jobs: InMemoryJobQueue, address: str
) -> None:
    async with limited_client(test_settings, jobs, api_read=1) as (_, http):
        (alice,) = await users(http, jobs, 1)
        target = f"{API}{address.replace('{me}', alice.user_id)}"

        first = await http.get(target, headers=alice.headers)
        second = await http.get(target, headers=alice.headers)

        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["ratelimit-limit"] == "1"
        assert code_of(second) == (429, "rate_limited")
        assert second.headers["cache-control"] == "no-store"


async def test_a_person_without_a_profile_row_is_hidden_from_follows_and_lists(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Сбой данных (нет строки профиля) не делает аккаунт «открытым»: для других его нет."""
    alice, ghost = await users(client, jobs, 2)
    await execute(
        admin_engine, "DELETE FROM profile.profiles WHERE user_id = :id", id=user_uuid(ghost)
    )

    followed = await follow(client, alice, ghost)

    assert code_of(followed) == (404, "not_found")
    for kind in ("followers", "following"):
        listed = await client.get(f"{API}/users/{ghost.user_id}/{kind}", headers=alice.headers)
        assert code_of(listed) == (404, "not_found"), kind
    assert await follow_rows(admin_engine) == []
    assert await graph_events(admin_engine) == []


async def test_a_follow_stuck_behind_the_profile_row_answers_503_and_goes_through_on_retry(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Пока владелец долго меняет профиль (открытие с тысячами запросов), подписка ждёт строку.

    У роли `app` `lock_timeout` 5 секунд: раньше затор давал `500 internal_error`, теперь `503`
    с `Retry-After`, а повтор после затора проходит как обычно.
    """
    owner, asker = await users(client, jobs, 2)

    async with profile_row_lock(admin_engine, owner):  # строка профиля `FOR UPDATE`
        stuck = await asyncio.wait_for(follow(client, asker, owner), timeout=20)

    assert code_of(stuck) == (503, "service_unavailable")
    assert stuck.headers["retry-after"] == "1"
    assert stuck.headers["cache-control"] == "no-store"
    assert await follow_rows(admin_engine) == []
    retry = await follow(client, asker, owner)
    assert retry.json() == {"status": "following"}
    assert await held_pair_locks(admin_engine) == 0
    await assert_graph_is_consistent(admin_engine)


async def test_database_errors_do_not_carry_the_values_of_the_query(
    test_settings: Settings,
) -> None:
    """⚖️ Текст ошибки БД попадает в журнал ошибок: в нём не должно быть значений параметров."""
    secret = "Иван Петров ivan.petrov@example.com"
    engine = create_engine(test_settings)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(
                    text("SELECT CAST(:value AS text), 1 / CAST(:zero AS integer)"),
                    {"value": secret, "zero": 0},
                )
    finally:
        await engine.dispose()

    message = str(caught.value)
    assert "division by zero" in message
    assert "SELECT CAST" in message  # текст запроса остаётся: по нему ищут причину
    assert secret not in message
    assert "ivan.petrov" not in message

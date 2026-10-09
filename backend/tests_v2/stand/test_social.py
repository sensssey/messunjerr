"""Дружба, блокировки (S7), подписки и поиск людей (S8) на стенде: настоящий API за Caddy, две реплики, общая БД.

Аккаунты постоянные (`stand-owner` и `stand-other`), поэтому каждый тест приводит пару в чистое
состояние до себя и после: прогон не должен оставлять их друзьями, заблокированными, подписанными,
с закрытым профилем или с висящей заявкой. Заявок в друзья в сутки можно отправить не больше 30 с
аккаунта (бакет `friend_request`), подписок 100 в час (`follow`), поисков 30 в минуту (`search`):
тесты берут по несколько запросов, а исчерпанный лимит заканчивается подсказкой про
`make stand-reset-limits`.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from .conftest import RESET_HINT, Account

API = "/api/v1"
REQUESTS = f"{API}/friend-requests"
FRIENDS = f"{API}/friends"
BLOCKS = f"{API}/blocks"
FOLLOWS = f"{API}/follows"
PROFILE = f"{API}/me/profile"
SEARCH = f"{API}/search/users"


async def pending_ids(
    client: httpx.AsyncClient, who: Account, direction: str, *, with_user: str
) -> list[str]:
    response = await client.get(REQUESTS, params={"direction": direction}, headers=who.headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"] if item["user"]["id"] == with_user]


async def reset_pair(client: httpx.AsyncClient, first: Account, second: Account) -> None:
    """Пара снова чужие: ни дружбы, ни блокировок, ни подписок, ни живых заявок, профили открыты.

    Всё через настоящий API."""
    await client.delete(f"{FRIENDS}/{second.user_id}", headers=first.headers)  # 404, если не друзья
    for blocker, blocked in ((first, second), (second, first)):
        done = await client.delete(f"{BLOCKS}/{blocked.user_id}", headers=blocker.headers)
        assert done.status_code == 204, done.text
    for sender, receiver in ((first, second), (second, first)):
        for request_id in await pending_ids(client, sender, "outgoing", with_user=receiver.user_id):
            done = await client.delete(f"{REQUESTS}/{request_id}", headers=sender.headers)
            assert done.status_code == 204, done.text
        # Снимает подписку или отзывает свой запрос на подписку; ответ всегда 204.
        done = await client.delete(f"{FOLLOWS}/{receiver.user_id}", headers=sender.headers)
        assert done.status_code == 204, done.text
    for person in (first, second):
        opened = await client.patch(PROFILE, json={"is_private": False}, headers=person.headers)
        assert opened.status_code == 200, opened.text


@pytest_asyncio.fixture
async def pair(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> AsyncGenerator[tuple[Account, Account]]:
    await reset_pair(client, account, other_account)
    try:
        yield account, other_account
    finally:
        await reset_pair(client, account, other_account)


async def send(
    client: httpx.AsyncClient, sender: Account, receiver: Account, **headers: str
) -> httpx.Response:
    response = await client.post(
        REQUESTS, json={"user_id": receiver.user_id}, headers={**sender.headers, **headers}
    )
    if response.status_code == 429:
        pytest.fail(RESET_HINT)
    return response


async def friend_ids(client: httpx.AsyncClient, who: Account) -> list[str]:
    response = await client.get(FRIENDS, headers=who.headers)
    assert response.status_code == 200, response.text
    return [item["user"]["id"] for item in response.json()["items"]]


async def relationship(client: httpx.AsyncClient, viewer: Account, other: Account) -> Any:
    response = await client.get(f"{API}/users/{other.user_id}", headers=viewer.headers)
    assert response.status_code == 200, response.text
    return response.json()["relationship"]


# ----------------------------------------------------------------------------- дружба
async def test_a_friendship_goes_from_the_request_to_the_removal_through_the_edge(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    owner, other = pair

    sent = await send(client, owner, other, **{"Idempotency-Key": str(uuid.uuid4())})
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["id"]
    assert sent.json()["direction"] == "outgoing"

    assert await pending_ids(client, other, "incoming", with_user=owner.user_id) == [request_id]
    assert (await relationship(client, owner, other))["friendship"] == "request_sent"
    assert (await relationship(client, other, owner))["friendship"] == "request_received"

    accepted = await client.post(f"{REQUESTS}/{request_id}/accept", headers=other.headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["friend"]["id"] == owner.user_id

    assert other.user_id in await friend_ids(client, owner)
    assert owner.user_id in await friend_ids(client, other)
    assert (await relationship(client, owner, other))["friendship"] == "friends"

    removed = await client.delete(f"{FRIENDS}/{other.user_id}", headers=owner.headers)
    assert removed.status_code == 204
    assert other.user_id not in await friend_ids(client, owner)
    assert other.user_id not in await friend_ids(client, other)


async def test_requests_sent_to_each_other_at_once_make_one_friendship_whatever_the_replica(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    """Две реплики API берут один замок в общей БД: встречные заявки дают одну дружбу."""
    owner, other = pair

    first, second = await asyncio.gather(send(client, owner, other), send(client, other, owner))

    assert sorted([first.status_code, second.status_code]) == [200, 201], (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]
    assert (await friend_ids(client, owner)).count(other.user_id) == 1
    assert (await friend_ids(client, other)).count(owner.user_id) == 1


# ----------------------------------------------------------------------------- блокировки
async def test_blocking_ends_the_friendship_and_hides_people_through_the_edge(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    owner, other = pair
    sent = await send(client, owner, other)
    assert sent.status_code == 201, sent.text
    accepted = await client.post(f"{REQUESTS}/{sent.json()['id']}/accept", headers=other.headers)
    assert accepted.status_code == 200, accepted.text

    blocked = await client.put(f"{BLOCKS}/{other.user_id}", headers=owner.headers)
    assert blocked.status_code == 204

    assert other.user_id not in await friend_ids(client, owner)
    for viewer, target in ((owner, other), (other, owner)):  # взаимная невидимость
        hidden = await client.get(f"{API}/users/{target.user_id}", headers=viewer.headers)
        assert hidden.status_code == 404, hidden.text
    refused = await send(client, other, owner)
    assert refused.status_code == 404, refused.text
    # Взаимной блокировки не бывает: заблокировавший скрыт, и блокировка в ответ отвечает как на скрытого.
    blocked_back = await client.put(f"{BLOCKS}/{owner.user_id}", headers=other.headers)
    assert blocked_back.status_code == 404, blocked_back.text
    listed = await client.get(f"{API}/me/blocks", headers=owner.headers)
    assert [item["user"]["id"] for item in listed.json()["items"]] == [other.user_id]
    assert (await client.get(f"{API}/me/blocks", headers=other.headers)).json()["items"] == []

    unblocked = await client.delete(f"{BLOCKS}/{other.user_id}", headers=owner.headers)
    assert unblocked.status_code == 204
    assert (await relationship(client, owner, other))["friendship"] == "none"  # дружба не вернулась


# ----------------------------------------------------------------------------- подписки (S8)
async def follow(client: httpx.AsyncClient, follower: Account, target: Account) -> httpx.Response:
    response = await client.put(f"{FOLLOWS}/{target.user_id}", headers=follower.headers)
    if response.status_code == 429:
        pytest.fail(RESET_HINT)
    return response


async def unfollow(client: httpx.AsyncClient, follower: Account, target: Account) -> None:
    done = await client.delete(f"{FOLLOWS}/{target.user_id}", headers=follower.headers)
    assert done.status_code == 204, done.text


async def my_people(client: httpx.AsyncClient, who: Account, kind: str) -> list[str]:
    """Идентификаторы из `GET /me/following` или `GET /me/followers` (первая страница)."""
    response = await client.get(f"{API}/me/{kind}", headers=who.headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def waiting_requests(client: httpx.AsyncClient, owner: Account) -> list[dict[str, Any]]:
    response = await client.get(f"{API}/me/follow-requests", headers=owner.headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def set_private(client: httpx.AsyncClient, person: Account, is_private: bool) -> None:
    done = await client.patch(PROFILE, json={"is_private": is_private}, headers=person.headers)
    assert done.status_code == 200, done.text


async def test_a_follow_of_an_open_profile_is_immediate_and_a_block_cuts_it_through_the_edge(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    owner, other = pair

    followed = await follow(client, owner, other)
    assert followed.status_code == 200, followed.text
    assert followed.json() == {"status": "following"}
    assert (await follow(client, owner, other)).json() == {
        "status": "following"
    }  # повтор безопасен
    assert (await relationship(client, owner, other))["following"] == "following"
    assert (await relationship(client, other, owner))["follows_you"] is True
    assert other.user_id in await my_people(client, owner, "following")
    assert owner.user_id in await my_people(client, other, "followers")

    await unfollow(client, owner, other)
    assert other.user_id not in await my_people(client, owner, "following")
    assert (await relationship(client, owner, other))["following"] == "none"

    await follow(client, owner, other)
    blocked = await client.put(f"{BLOCKS}/{owner.user_id}", headers=other.headers)
    assert blocked.status_code == 204, blocked.text  # блокирует тот, на кого подписаны
    assert owner.user_id not in await my_people(client, other, "followers")
    unblocked = await client.delete(f"{BLOCKS}/{owner.user_id}", headers=other.headers)
    assert unblocked.status_code == 204
    assert (await relationship(client, owner, other))[
        "following"
    ] == "none"  # подписка не вернулась


async def test_a_follow_of_a_private_profile_waits_for_the_owner_and_opening_approves_it(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    owner, other = pair
    await set_private(client, other, True)

    asked = await follow(client, owner, other)
    assert asked.json() == {"status": "requested"}, asked.text
    assert (await relationship(client, owner, other))["following"] == "requested"
    (request,) = await waiting_requests(client, other)
    assert request["user"]["id"] == owner.user_id
    me = await client.get(f"{API}/me", headers=other.headers)
    assert me.json()["counters"]["pending_follow_requests"] == 1

    approved = await client.post(
        f"{API}/me/follow-requests/{request['id']}/approve", headers=other.headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["follower"]["id"] == owner.user_id
    assert owner.user_id in await my_people(client, other, "followers")
    again = await client.post(
        f"{API}/me/follow-requests/{request['id']}/approve", headers=other.headers
    )
    assert again.status_code == 409, again.text

    await unfollow(client, owner, other)
    assert (await follow(client, owner, other)).json() == {"status": "requested"}
    (request,) = await waiting_requests(client, other)
    declined = await client.post(
        f"{API}/me/follow-requests/{request['id']}/decline", headers=other.headers
    )
    assert declined.status_code == 204, declined.text
    assert await waiting_requests(client, other) == []

    assert (await follow(client, owner, other)).json() == {"status": "requested"}
    await set_private(client, other, False)  # открытие профиля одобряет ждущие запросы
    assert await waiting_requests(client, other) == []
    assert owner.user_id in await my_people(client, other, "followers")


async def test_a_follow_racing_the_opening_of_the_profile_leaves_no_waiting_request(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    """Подписка и открытие профиля берут строку профиля и идут друг за другом, на любой реплике."""
    owner, other = pair
    for _ in range(5):
        await set_private(client, other, True)
        followed, opened = await asyncio.gather(
            follow(client, owner, other),
            client.patch(PROFILE, json={"is_private": False}, headers=other.headers),
        )
        assert followed.status_code == 200, followed.text
        assert followed.json()["status"] in {"following", "requested"}
        assert opened.status_code == 200, opened.text
        assert owner.user_id in await my_people(client, other, "followers")
        assert await waiting_requests(client, other) == []
        await unfollow(client, owner, other)


# ----------------------------------------------------------------------------- поиск людей (S8)
async def test_people_search_goes_through_the_edge_and_respects_blocks(
    client: httpx.AsyncClient, pair: tuple[Account, Account]
) -> None:
    owner, other = pair

    found = await client.get(SEARCH, params={"q": "stand_other"}, headers=owner.headers)
    if found.status_code == 429:
        pytest.fail(RESET_HINT)
    assert found.status_code == 200, found.text
    assert "ratelimit-limit" in found.headers
    assert found.headers["cache-control"] == "no-store"
    items = found.json()["items"]
    assert items[0]["user"]["id"] == other.user_id  # точный ник первым
    assert items[0]["relationship"]["friendship"] == "none"
    assert owner.user_id not in [item["user"]["id"] for item in items]  # себя не находят

    blocked = await client.put(f"{BLOCKS}/{other.user_id}", headers=owner.headers)
    assert blocked.status_code == 204, blocked.text
    for viewer, query in ((owner, "stand_other"), (other, "stand_owner")):
        hidden = await client.get(SEARCH, params={"q": query}, headers=viewer.headers)
        assert hidden.status_code == 200, hidden.text
        assert {owner.user_id, other.user_id}.isdisjoint(
            item["user"]["id"] for item in hidden.json()["items"]
        )

    too_deep = await client.get(
        SEARCH, params={"q": "stand", "limit": 50, "offset": 180}, headers=owner.headers
    )
    assert too_deep.status_code == 422, too_deep.text
    assert too_deep.json()["errors"][0]["code"] == "out_of_range"

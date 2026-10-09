"""Помощники тестов социального графа (S7, S8): заявки, дружба, блокировки, подписки, события, строки таблиц."""

import base64
import json
import uuid
from collections import Counter
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import SignedInUser, execute, fetch_all, verified_user

API = "/api/v1"
FRIEND_REQUESTS = f"{API}/friend-requests"
FRIENDS = f"{API}/friends"
BLOCKS = f"{API}/blocks"
MY_BLOCKS = f"{API}/me/blocks"
FOLLOWS = f"{API}/follows"
MY_FOLLOWING = f"{API}/me/following"
MY_FOLLOWERS = f"{API}/me/followers"
FOLLOW_REQUESTS = f"{API}/me/follow-requests"
GRAPH_TOPIC = "mj.social.graph.v1"
SUMMARY_KEYS = {"id", "username", "display_name", "avatar"}


def user_uuid(user: SignedInUser) -> uuid.UUID:
    return uuid.UUID(user.user_id)


def forged_cursor(payload: Any) -> str:
    """Курсор из произвольной полезной нагрузки: так выглядит подделка, которую пришлёт злоумышленник."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def renamed_key(shape: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Ключ страницы под имя поля списка; `{id}` заменяется настоящим идентификатором."""
    return {
        (field_name if name == "key" else name): (str(uuid.uuid4()) if value == "{id}" else value)
        for name, value in shape.items()
    }


def code_of(response: httpx.Response) -> tuple[int, str]:
    """Статус и машинный код ошибки из `application/problem+json`."""
    return response.status_code, response.json()["code"]


async def send_request(
    client: httpx.AsyncClient, sender: SignedInUser, target: SignedInUser | str, **headers: str
) -> httpx.Response:
    """`POST /friend-requests`; цель можно передать пользователем или строкой-идентификатором."""
    target_id = target if isinstance(target, str) else target.user_id
    return await client.post(
        FRIEND_REQUESTS,
        json={"user_id": target_id},
        headers={**sender.headers, **headers},
    )


async def accept(
    client: httpx.AsyncClient, receiver: SignedInUser, request_id: str
) -> httpx.Response:
    return await client.post(f"{FRIEND_REQUESTS}/{request_id}/accept", headers=receiver.headers)


async def decline(
    client: httpx.AsyncClient, receiver: SignedInUser, request_id: str
) -> httpx.Response:
    return await client.post(f"{FRIEND_REQUESTS}/{request_id}/decline", headers=receiver.headers)


async def cancel(
    client: httpx.AsyncClient, sender: SignedInUser, request_id: str
) -> httpx.Response:
    return await client.delete(f"{FRIEND_REQUESTS}/{request_id}", headers=sender.headers)


async def befriend(client: httpx.AsyncClient, first: SignedInUser, second: SignedInUser) -> None:
    """Дружба обычным путём: первый отправляет заявку, второй принимает."""
    sent = await send_request(client, first, second)
    assert sent.status_code == 201, sent.text
    accepted = await accept(client, second, sent.json()["id"])
    assert accepted.status_code == 200, accepted.text


async def block(
    client: httpx.AsyncClient, blocker: SignedInUser, target: SignedInUser | str
) -> httpx.Response:
    target_id = target if isinstance(target, str) else target.user_id
    return await client.put(f"{BLOCKS}/{target_id}", headers=blocker.headers)


async def unblock(
    client: httpx.AsyncClient, blocker: SignedInUser, target: SignedInUser | str
) -> httpx.Response:
    target_id = target if isinstance(target, str) else target.user_id
    return await client.delete(f"{BLOCKS}/{target_id}", headers=blocker.headers)


async def users(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, count: int
) -> list[SignedInUser]:
    return [await verified_user(client, jobs) for _ in range(count)]


# ----------------------------------------------------------------------------- подписки (S8)
async def follow(
    client: httpx.AsyncClient, follower: SignedInUser, target: SignedInUser | str
) -> httpx.Response:
    """`PUT /follows/{user_id}`."""
    target_id = target if isinstance(target, str) else target.user_id
    return await client.put(f"{FOLLOWS}/{target_id}", headers=follower.headers)


async def unfollow(
    client: httpx.AsyncClient, follower: SignedInUser, target: SignedInUser | str
) -> httpx.Response:
    """`DELETE /follows/{user_id}`."""
    target_id = target if isinstance(target, str) else target.user_id
    return await client.delete(f"{FOLLOWS}/{target_id}", headers=follower.headers)


async def remove_follower_of(
    client: httpx.AsyncClient, owner: SignedInUser, follower: SignedInUser | str
) -> httpx.Response:
    """`DELETE /me/followers/{user_id}`."""
    follower_id = follower if isinstance(follower, str) else follower.user_id
    return await client.delete(f"{MY_FOLLOWERS}/{follower_id}", headers=owner.headers)


async def approve_follow(
    client: httpx.AsyncClient, owner: SignedInUser, request_id: str
) -> httpx.Response:
    return await client.post(f"{FOLLOW_REQUESTS}/{request_id}/approve", headers=owner.headers)


async def decline_follow(
    client: httpx.AsyncClient, owner: SignedInUser, request_id: str
) -> httpx.Response:
    return await client.post(f"{FOLLOW_REQUESTS}/{request_id}/decline", headers=owner.headers)


async def patch_private(
    client: httpx.AsyncClient, user: SignedInUser, is_private: bool
) -> httpx.Response:
    """`PATCH /me/profile` с одним полем `is_private` (остальной профиль не трогается)."""
    return await client.patch(
        f"{API}/me/profile", json={"is_private": is_private}, headers=user.headers
    )


async def set_private(client: httpx.AsyncClient, user: SignedInUser, is_private: bool) -> None:
    response = await patch_private(client, user, is_private)
    assert response.status_code == 200, response.text
    assert response.json()["is_private"] is is_private


async def follow_privately(
    client: httpx.AsyncClient, follower: SignedInUser, owner: SignedInUser
) -> str:
    """Подписка на закрытый профиль с одобрением: возвращает идентификатор запроса.

    Профиль владельца должен быть закрыт: иначе подписка пройдёт сразу и запроса не будет.
    """
    answer = await follow(client, follower, owner)
    assert answer.json() == {"status": "requested"}, answer.text
    waiting = await incoming_follow_requests(client, owner)
    (request_id,) = [item["id"] for item in waiting if item["user"]["id"] == follower.user_id]
    approved = await approve_follow(client, owner, request_id)
    assert approved.status_code == 200, approved.text
    return request_id


async def _all_pages(
    client: httpx.AsyncClient, address: str, user: SignedInUser, **query: Any
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, **query}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(address, params=params, headers=user.headers)
        assert response.status_code == 200, response.text
        page = response.json()
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return found


async def following_ids(client: httpx.AsyncClient, user: SignedInUser, **query: Any) -> list[str]:
    """Идентификаторы из `GET /me/following` (все страницы)."""
    return [item["id"] for item in await _all_pages(client, MY_FOLLOWING, user, **query)]


async def follower_ids(client: httpx.AsyncClient, user: SignedInUser, **query: Any) -> list[str]:
    """Идентификаторы из `GET /me/followers` (все страницы)."""
    return [item["id"] for item in await _all_pages(client, MY_FOLLOWERS, user, **query)]


async def incoming_follow_requests(
    client: httpx.AsyncClient, user: SignedInUser, **query: Any
) -> list[dict[str, Any]]:
    """Записи из `GET /me/follow-requests` (все страницы)."""
    return await _all_pages(client, FOLLOW_REQUESTS, user, **query)


async def follow_request_ids(client: httpx.AsyncClient, user: SignedInUser) -> list[str]:
    return [item["id"] for item in await incoming_follow_requests(client, user)]


async def user_follow_list(
    client: httpx.AsyncClient, viewer: SignedInUser, ref: str, kind: str, **query: Any
) -> httpx.Response:
    """`GET /users/{ref}/followers` или `/following` (`kind`), одна страница."""
    return await client.get(f"{API}/users/{ref}/{kind}", params=query, headers=viewer.headers)


async def friend_ids(client: httpx.AsyncClient, user: SignedInUser, **query: Any) -> list[str]:
    """Идентификаторы друзей из `GET /friends` (все страницы)."""
    found: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, **query}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(FRIENDS, params=params, headers=user.headers)).json()
        found.extend(item["user"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return found


async def blocked_ids(client: httpx.AsyncClient, user: SignedInUser, **query: Any) -> list[str]:
    """Идентификаторы из `GET /me/blocks` (все страницы)."""
    found: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, **query}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(MY_BLOCKS, params=params, headers=user.headers)).json()
        found.extend(item["user"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return found


async def request_ids(client: httpx.AsyncClient, user: SignedInUser, direction: str) -> list[str]:
    """Идентификаторы заявок из `GET /friend-requests` (все страницы) для одного направления."""
    found: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, "direction": direction}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(FRIEND_REQUESTS, params=params, headers=user.headers)).json()
        found.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return found


async def graph_events(engine: AsyncEngine) -> list[dict[str, Any]]:
    """События графа из outbox по порядку: тип, ключ партиции, данные и заголовки (в них актёр)."""
    return await fetch_all(
        engine,
        "SELECT event_type, key, payload, headers FROM platform.outbox WHERE topic = :topic ORDER BY id",
        topic=GRAPH_TOPIC,
    )


async def friendship_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine,
        "SELECT user_low_id, user_high_id, created_at FROM social.friendships ORDER BY created_at",
    )


async def request_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine,
        "SELECT id, sender_id, receiver_id, status, responded_at FROM social.friend_requests "
        "ORDER BY created_at, id",
    )


async def block_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine, "SELECT blocker_id, blocked_id FROM social.blocks ORDER BY created_at, blocker_id"
    )


async def follow_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine,
        "SELECT follower_id, followee_id, created_at FROM social.follows "
        "ORDER BY created_at, follower_id, followee_id",
    )


async def follow_request_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine,
        "SELECT id, follower_id, followee_id, status, responded_at FROM social.follow_requests "
        "ORDER BY created_at, id",
    )


async def assert_graph_is_consistent(engine: AsyncEngine) -> None:
    """Инварианты графа (4.6), которые не должны нарушаться ни при каком порядке операций.

    Блокировка в любую сторону несовместима с дружбой, с ожидающей заявкой пары, с подпиской и с
    ждущим запросом на подписку; ожидающая заявка несовместима с дружбой; ждущий запрос на подписку
    несовместим с готовой подпиской и не бывает у открытого профиля (S8). Остальное (одна ожидающая
    заявка на пару, порядок пары, самоссылки) держат ограничения БД.
    """
    friends_and_blocks = await fetch_all(
        engine,
        "SELECT f.user_low_id, f.user_high_id FROM social.friendships f "
        "JOIN social.blocks b ON (b.blocker_id = f.user_low_id AND b.blocked_id = f.user_high_id) "
        "OR (b.blocker_id = f.user_high_id AND b.blocked_id = f.user_low_id)",
    )
    assert friends_and_blocks == [], "дружба и блокировка одной пары вместе"
    pending_with_friends = await fetch_all(
        engine,
        "SELECT r.id FROM social.friend_requests r JOIN social.friendships f "
        "ON f.user_low_id = LEAST(r.sender_id, r.receiver_id) "
        "AND f.user_high_id = GREATEST(r.sender_id, r.receiver_id) WHERE r.status = 'pending'",
    )
    assert pending_with_friends == [], "ожидающая заявка у людей, которые уже друзья"
    pending_with_blocks = await fetch_all(
        engine,
        "SELECT r.id FROM social.friend_requests r JOIN social.blocks b "
        "ON (b.blocker_id = r.sender_id AND b.blocked_id = r.receiver_id) "
        "OR (b.blocker_id = r.receiver_id AND b.blocked_id = r.sender_id) "
        "WHERE r.status = 'pending'",
    )
    assert pending_with_blocks == [], "ожидающая заявка между заблокированными"
    follows_and_blocks = await fetch_all(
        engine,
        "SELECT f.follower_id FROM social.follows f JOIN social.blocks b "
        "ON (b.blocker_id = f.follower_id AND b.blocked_id = f.followee_id) "
        "OR (b.blocker_id = f.followee_id AND b.blocked_id = f.follower_id)",
    )
    assert follows_and_blocks == [], "подписка и блокировка одной пары вместе"
    follow_requests_and_blocks = await fetch_all(
        engine,
        "SELECT r.id FROM social.follow_requests r JOIN social.blocks b "
        "ON (b.blocker_id = r.follower_id AND b.blocked_id = r.followee_id) "
        "OR (b.blocker_id = r.followee_id AND b.blocked_id = r.follower_id) "
        "WHERE r.status = 'pending'",
    )
    assert follow_requests_and_blocks == [], "ждущий запрос на подписку между заблокированными"
    requests_and_follows = await fetch_all(
        engine,
        "SELECT r.id FROM social.follow_requests r JOIN social.follows f "
        "ON f.follower_id = r.follower_id AND f.followee_id = r.followee_id "
        "WHERE r.status = 'pending'",
    )
    assert requests_and_follows == [], "ждущий запрос у человека, который уже подписан"
    requests_to_open_profiles = await fetch_all(
        engine,
        "SELECT r.id FROM social.follow_requests r JOIN profile.profiles p "
        "ON p.user_id = r.followee_id WHERE r.status = 'pending' AND NOT p.is_private",
    )
    assert requests_to_open_profiles == [], "ждущий запрос к открытому профилю"


async def assert_events_explain_follows(engine: AsyncEngine) -> None:
    """События подписок объясняют таблицы (S8-05): по каждой паре создано минус снято равно тому, что осталось.

    Подписку создают `FollowCreated` и одобрение (`FollowRequestResponded` с `approved`), снимают
    `FollowRemoved` (отписка, удаление подписчика, блокировка). Запросов записано столько, сколько
    строк, а исходов `approved` и `declined` столько, сколько строк с этими статусами. Проверка
    годится только для состояния, созданного через API: строки, вставленные в обход команд, событий не имеют.
    """
    balance: Counter[tuple[str, str]] = Counter()
    asked = approved = declined = 0
    for row in await graph_events(engine):
        kind, payload = row["event_type"], row["payload"]
        if not kind.startswith("Follow"):
            continue
        pair = (payload["follower_id"], payload["followee_id"])
        if kind == "FollowCreated":
            balance[pair] += 1
        elif kind == "FollowRemoved":
            balance[pair] -= 1
        elif kind == "FollowRequested":
            asked += 1
        elif payload["decision"] == "approved":
            balance[pair] += 1
            approved += 1
        else:
            declined += 1
    assert all(count in (0, 1) for count in balance.values()), balance
    present = {(str(r["follower_id"]), str(r["followee_id"])) for r in await follow_rows(engine)}
    assert {pair for pair, count in balance.items() if count == 1} == present
    requests = await follow_request_rows(engine)
    assert asked == len(requests), "число FollowRequested не равно числу запросов"
    assert approved == sum(1 for r in requests if r["status"] == "approved")
    assert declined == sum(1 for r in requests if r["status"] == "declined")


async def set_status(engine: AsyncEngine, user: SignedInUser, status: str) -> None:
    """Меняет статус аккаунта напрямую в БД (так делают модерация и удаление аккаунта)."""
    await execute(
        engine,
        "UPDATE identity.users SET status = :status WHERE id = :id",
        status=status,
        id=user_uuid(user),
    )

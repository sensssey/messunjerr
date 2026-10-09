"""Модель графа (S7-07, S8-06): случайные последовательности операций против простой модели в памяти.

Модель знает правила 4.6 и 5.4 в самом коротком виде: дружбы, блокировки, заявки в друзья, подписки и
запросы на подписку как множества и словари, плюс те, чьи аккаунты не `active`, и закрытые профили, и
ответ на каждую операцию. На каждом шаге настоящий API отвечает как модель, а списки, отношения,
счётчики людей и доступ к чужим спискам совпадают с её состоянием; в конце проверяются инварианты
базы и события подписок (модель ведёт свой журнал: тип, кто совершил, кто на кого). Ход случайный,
но от зерна: упавший тест повторяется тем же зерном, а в сообщении лежат последние шаги.
"""

import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, SignedInUser, fill_profile, set_privacy, url, view
from .social_helpers import (
    FRIENDS,
    accept,
    approve_follow,
    assert_events_explain_follows,
    assert_graph_is_consistent,
    block,
    blocked_ids,
    cancel,
    decline,
    decline_follow,
    follow,
    follow_request_ids,
    follow_request_rows,
    follower_ids,
    following_ids,
    friend_ids,
    graph_events,
    patch_private,
    remove_follower_of,
    request_ids,
    send_request,
    set_status,
    unblock,
    unfollow,
    users,
)

OPERATIONS = (
    "send",
    "accept",
    "decline",
    "cancel",
    "remove_friend",
    "block",
    "unblock",
    "deactivate",
    "reactivate",
    "follow",
    "unfollow",
    "remove_follower",
    "approve",
    "reject",
    "toggle_private",
)
WEIGHTS = (20, 8, 5, 5, 6, 5, 8, 2, 6, 30, 9, 6, 10, 5, 5)
RESPONSES = {"accept", "decline", "cancel"}
FOLLOW_RESPONSES = {"approve", "reject"}
RESULT_STATUS = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}
PEOPLE = 4
STEPS = 80
LISTS_EVERY = 7
"""Чужие списки и общие друзья сверяются не на каждом шаге: они дороже остального."""
CITY = "Казань"
"""Город, который все заполнили в профиле: закрытый профиль скрывает его от тех, кто не друг и не подписчик."""

type FollowEvent = tuple[str, str, str, str, str | None]
"""Событие подписок как его видно в outbox: тип, кто совершил, подписчик, на кого, решение."""


@dataclass
class Request:
    sender: str
    receiver: str
    status: str = "pending"


@dataclass
class FollowAsk:
    follower: str
    followee: str
    status: str = "pending"


@dataclass
class Model:
    friends: set[frozenset[str]] = field(default_factory=set[frozenset[str]])
    blocks: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    requests: dict[str, Request] = field(default_factory=dict[str, Request])
    inactive: set[str] = field(default_factory=set[str])
    private: set[str] = field(default_factory=set[str])
    follows: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    asks: dict[str, FollowAsk] = field(default_factory=dict[str, FollowAsk])
    follow_events: list[FollowEvent] = field(default_factory=list[FollowEvent])

    def blocked(self, first: str, second: str) -> bool:
        return (first, second) in self.blocks or (second, first) in self.blocks

    def hidden_from(self, viewer: str, other: str) -> bool:
        """Человека для зрителя нет: аккаунт не `active` либо между ними блокировка."""
        return other in self.inactive or self.blocked(viewer, other)

    def pending_between(self, first: str, second: str) -> str | None:
        for request_id, request in self.requests.items():
            if request.status == "pending" and {request.sender, request.receiver} == {
                first,
                second,
            }:
                return request_id
        return None

    def latest_between(self, first: str, second: str) -> str | None:
        found = [
            request_id
            for request_id, request in self.requests.items()
            if {request.sender, request.receiver} == {first, second}
        ]
        return found[-1] if found else None  # словарь хранит порядок появления

    def friends_of(self, person: str) -> set[str]:
        """Друзья, которых показывают списки и счётчики: только аккаунты `active`."""
        return {
            friend
            for pair in self.friends
            if person in pair
            for friend in pair - {person}
            if friend not in self.inactive
        }

    def blocked_by(self, person: str) -> set[str]:
        return {blocked for blocker, blocked in self.blocks if blocker == person}

    def pending_ids(self, person: str, direction: str) -> set[str]:
        """Ждущие заявки человека, у которых вторая сторона `active`."""
        found: set[str] = set()
        for request_id, request in self.requests.items():
            if request.status != "pending":
                continue
            mine, theirs = (
                (request.receiver, request.sender)
                if direction == "incoming"
                else (request.sender, request.receiver)
            )
            if mine == person and theirs not in self.inactive:
                found.add(request_id)
        return found

    def relation(self, viewer: str, other: str) -> str | None:
        """Ожидаемое `relationship.friendship`; `None`, когда человека для зрителя нет."""
        if self.hidden_from(viewer, other):
            return None
        if frozenset((viewer, other)) in self.friends:
            return "friends"
        pending = self.pending_between(viewer, other)
        if pending is None:
            return "none"
        return "request_sent" if self.requests[pending].sender == viewer else "request_received"

    def respond_status(self, operation: str, actor: str, request_id: str) -> int:
        """Ответ на accept/decline/cancel: чужая, неизвестная или от скрытого заявка 404, закрытая 409."""
        request = self.requests.get(request_id)
        if request is None:
            return 404
        allowed, counterpart = (
            (request.sender, request.receiver)
            if operation == "cancel"
            else (request.receiver, request.sender)
        )
        if actor != allowed or counterpart in self.inactive:
            return 404
        if request.status != "pending":
            return 409
        return 200 if operation == "accept" else 204

    def send_status(self, actor: str, other: str) -> int:
        pending = self.pending_between(actor, other)
        if self.hidden_from(actor, other):
            return 404
        if frozenset((actor, other)) in self.friends:
            return 409
        if pending is not None and self.requests[pending].sender == actor:
            return 409
        if pending is not None:
            return 200  # встречная заявка принимается сразу
        return 201

    def block_status(self, actor: str, other: str) -> int:
        if (actor, other) in self.blocks:
            return 204  # повтор: ничего не меняется, даже если человек с тех пор ушёл
        if other in self.inactive or (other, actor) in self.blocks:
            return 404  # скрытого нельзя заблокировать, взаимной блокировки не бывает
        return 204

    # --- подписки (S8)
    def waiting_ask(self, follower: str, followee: str) -> str | None:
        for ask_id, ask in self.asks.items():
            if ask.status == "pending" and (ask.follower, ask.followee) == (follower, followee):
                return ask_id
        return None

    def latest_ask(self, follower: str, followee: str) -> str | None:
        found = [
            ask_id
            for ask_id, ask in self.asks.items()
            if (ask.follower, ask.followee) == (follower, followee)
        ]
        return found[-1] if found else None

    def following_of(self, person: str) -> set[str]:
        """На кого подписан человек; в списках и счётчиках только аккаунты `active`."""
        return {b for a, b in self.follows if a == person and b not in self.inactive}

    def followers_of(self, person: str) -> set[str]:
        return {a for a, b in self.follows if b == person and a not in self.inactive}

    def following_state(self, viewer: str, other: str) -> str:
        if (viewer, other) in self.follows:
            return "following"
        return "requested" if self.waiting_ask(viewer, other) is not None else "none"

    def waiting_asks_of(self, person: str) -> list[str]:
        """Ждущие запросы к человеку по возрастанию просившего (так их одобряет открытие профиля)."""
        found = [
            (ask.follower, ask_id)
            for ask_id, ask in self.asks.items()
            if ask.status == "pending" and ask.followee == person
        ]
        return [ask_id for _, ask_id in sorted(found)]

    def visible_waiting_asks(self, person: str) -> set[str]:
        return {
            ask_id
            for ask_id in self.waiting_asks_of(person)
            if self.asks[ask_id].follower not in self.inactive
        }

    def follow_outcome(self, actor: str, other: str) -> tuple[int, str | None, str | None]:
        """Код ответа, `status` в теле и что появилось нового: `follow`, `request` или ничего."""
        if self.hidden_from(actor, other):
            return 404, None, None
        if (actor, other) in self.follows:
            return 200, "following", None
        if self.waiting_ask(actor, other) is not None:
            return 200, "requested", None
        if other in self.private:
            return 200, "requested", "request"
        return 200, "following", "follow"

    def respond_follow_status(self, operation: str, actor: str, ask_id: str) -> int:
        ask = self.asks.get(ask_id)
        if ask is None or ask.followee != actor or ask.follower in self.inactive:
            return 404
        if ask.status != "pending":
            return 409
        return 200 if operation == "approve" else 204

    def sees_details(self, viewer: str, owner: str) -> bool:
        """Подробности закрытого профиля видят владелец, друзья и подписчики; открытого все."""
        return (
            owner not in self.private
            or viewer == owner
            or frozenset((viewer, owner)) in self.friends
            or (viewer, owner) in self.follows
        )

    def list_status(self, viewer: str, owner: str) -> int:
        """Доступ к спискам подписчиков и подписок чужого профиля (все выставили `everyone`)."""
        if self.hidden_from(viewer, owner):
            return 404
        if viewer != owner and not self.sees_details(viewer, owner):
            return 403
        return 200

    def log(
        self, kind: str, actor: str, follower: str, followee: str, decision: str | None = None
    ) -> None:
        self.follow_events.append((kind, actor, follower, followee, decision))


@dataclass
class Walk:
    client: httpx.AsyncClient
    engine: AsyncEngine
    order: list[SignedInUser]
    model: Model = field(default_factory=Model)
    log: list[str] = field(default_factory=list[str])
    outcomes: Counter[tuple[str, int]] = field(default_factory=Counter[tuple[str, int]])

    def context(self) -> str:
        return "\n".join(["последние шаги:", *self.log[-8:]])

    def active(self) -> list[SignedInUser]:
        return [user for user in self.order if user.user_id not in self.model.inactive]

    def choose(self, rng: random.Random) -> tuple[str, SignedInUser, SignedInUser, str]:
        """Операция, кто её делает (всегда `active`), над кем и по какой заявке (иногда неизвестной)."""
        operation = rng.choices(OPERATIONS, WEIGHTS)[0]
        actor = rng.choice(self.active())
        other = rng.choice([user for user in self.order if user is not actor])
        model = self.model
        if operation in FOLLOW_RESPONSES:  # отвечает владелец, у которого есть кому отвечать
            owners = [u for u in self.active() if model.visible_waiting_asks(u.user_id)]
            actor = self.leaning(rng, owners, actor)
            other = rng.choice([user for user in self.order if user is not actor])
        # Чтобы случайный ход чаще менял состояние, а не повторял бесполезные вызовы, подписки,
        # отписки и удаления подписчиков чаще направлены на тех, к кому они применимы.
        others = [user for user in self.order if user is not actor]
        if operation == "follow":
            other = self.leaning(
                rng,
                [
                    u
                    for u in others
                    if (actor.user_id, u.user_id) not in model.follows
                    and model.waiting_ask(actor.user_id, u.user_id) is None
                    and not model.hidden_from(actor.user_id, u.user_id)
                ],
                other,
            )
        elif operation == "unfollow":
            other = self.leaning(
                rng,
                [
                    u
                    for u in others
                    if (actor.user_id, u.user_id) in model.follows
                    or model.waiting_ask(actor.user_id, u.user_id) is not None
                ],
                other,
            )
        elif operation == "remove_follower":
            other = self.leaning(
                rng, [u for u in others if (u.user_id, actor.user_id) in model.follows], other
            )
        request_id = ""
        if operation in RESPONSES:
            known = self.model.latest_between(actor.user_id, other.user_id)
            unknown = known is None or rng.random() < 0.05  # заявки нет либо проверяем чужой номер
            request_id = str(uuid.uuid4()) if unknown else str(known)
        elif operation in FOLLOW_RESPONSES:
            # Отвечает владелец, `other` это тот, кто просил (чаще из тех, чей запрос ждёт: иначе
            # почти все ответы упираются в «нет такого запроса»).
            askers = {
                self.model.asks[i].follower for i in model.visible_waiting_asks(actor.user_id)
            }
            other = self.leaning(rng, [u for u in self.order if u.user_id in askers], other)
            known = self.model.latest_ask(other.user_id, actor.user_id)
            unknown = known is None or rng.random() < 0.05
            request_id = str(uuid.uuid4()) if unknown else str(known)
        return operation, actor, other, request_id

    @staticmethod
    def leaning(
        rng: random.Random, suitable: list[SignedInUser], fallback: SignedInUser
    ) -> SignedInUser:
        """Чаще (в 85% случаев) берёт подходящего человека, иначе случайного: отказы тоже нужны."""
        if suitable and rng.random() < 0.85:
            return rng.choice(suitable)
        return fallback

    async def learn_ask(self, follower: str, followee: str) -> None:
        """Идентификатор нового запроса на подписку берётся из базы: API его просившему не отдаёт."""
        rows = [
            row
            for row in await follow_request_rows(self.engine)
            if (str(row["follower_id"]), str(row["followee_id"])) == (follower, followee)
            and row["status"] == "pending"
        ]
        assert len(rows) == 1, self.context()
        self.model.asks[str(rows[0]["id"])] = FollowAsk(follower, followee)

    async def apply(
        self, operation: str, user: SignedInUser, target: SignedInUser, request_id: str
    ) -> None:
        model = self.model
        actor, other = user.user_id, target.user_id
        self.log.append(f"{operation}: {actor[-6:]} -> {other[-6:]} заявка {request_id[-6:]}")
        status = 0
        match operation:
            case "send":
                expected = model.send_status(actor, other)
                pending = model.pending_between(actor, other)
                response = await send_request(self.client, user, target)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                if expected == 201:
                    model.requests[response.json()["id"]] = Request(sender=actor, receiver=other)
                elif expected == 200:
                    assert response.json()["id"] == pending, self.context()
                    assert pending is not None
                    model.requests[pending].status = "accepted"
                    model.friends.add(frozenset((actor, other)))
            case "accept" | "decline" | "cancel":
                expected = model.respond_status(operation, actor, request_id)
                call = {"accept": accept, "decline": decline, "cancel": cancel}[operation]
                response = await call(self.client, user, request_id)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                if expected in (200, 204):
                    request = model.requests[request_id]
                    request.status = RESULT_STATUS[operation]
                    if operation == "accept":
                        model.friends.add(frozenset((request.sender, request.receiver)))
            case "remove_friend":
                pair = frozenset((actor, other))
                expected = 204 if pair in model.friends else 404
                response = await self.client.delete(f"{FRIENDS}/{other}", headers=user.headers)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                model.friends.discard(pair)
            case "block":
                expected = model.block_status(actor, other)
                fresh = (actor, other) not in model.blocks
                response = await block(self.client, user, target)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                if expected == 204 and fresh:
                    model.blocks.add((actor, other))
                    model.friends.discard(frozenset((actor, other)))
                    pending = model.pending_between(actor, other)
                    if pending is not None:
                        model.requests[pending].status = "cancelled"
                    # Блокировка рвёт подписки в обе стороны (события по возрастанию подписчика)
                    # и закрывает ждущие запросы на подписку без событий.
                    for follower, followee in sorted(
                        pair for pair in model.follows if set(pair) == {actor, other}
                    ):
                        model.follows.discard((follower, followee))
                        model.log("FollowRemoved", actor, follower, followee)
                    for ask in model.asks.values():
                        if ask.status == "pending" and {ask.follower, ask.followee} == {
                            actor,
                            other,
                        }:
                            ask.status = "cancelled"
            case "unblock":
                response = await unblock(self.client, user, target)
                status = response.status_code
                assert status == 204, (response.text, self.context())
                model.blocks.discard((actor, other))
            case "deactivate":
                await set_status(self.engine, target, "suspended")
                model.inactive.add(other)
            case "reactivate":
                await set_status(self.engine, target, "active")
                model.inactive.discard(other)
            case "follow":
                expected, body_status, new = model.follow_outcome(actor, other)
                response = await follow(self.client, user, target)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                if expected == 200:
                    assert response.json() == {"status": body_status}, self.context()
                if new == "follow":
                    model.follows.add((actor, other))
                    model.log("FollowCreated", actor, actor, other)
                elif new == "request":
                    await self.learn_ask(actor, other)
                    model.log("FollowRequested", actor, actor, other)
            case "unfollow":
                response = await unfollow(self.client, user, target)
                status = response.status_code
                assert status == 204, (response.text, self.context())
                if (actor, other) in model.follows:
                    model.follows.discard((actor, other))
                    model.log("FollowRemoved", actor, actor, other)
                waiting = model.waiting_ask(actor, other)
                if waiting is not None:
                    model.asks[waiting].status = "cancelled"  # отмена запроса без события
            case "remove_follower":
                response = await remove_follower_of(self.client, user, target)
                status = response.status_code
                assert status == 204, (response.text, self.context())
                if (other, actor) in model.follows:
                    model.follows.discard((other, actor))
                    model.log("FollowRemoved", actor, other, actor)
            case "approve" | "reject":
                expected = model.respond_follow_status(operation, actor, request_id)
                call = approve_follow if operation == "approve" else decline_follow
                response = await call(self.client, user, request_id)
                status = response.status_code
                assert status == expected, (response.text, self.context())
                if expected in (200, 204):
                    ask = model.asks[request_id]
                    if operation == "approve":
                        ask.status = "approved"
                        model.follows.add((ask.follower, ask.followee))
                        model.log("FollowRequestResponded", actor, ask.follower, actor, "approved")
                        assert response.json()["follower"]["id"] == ask.follower, self.context()
                    else:
                        ask.status = "declined"
                        model.log("FollowRequestResponded", actor, ask.follower, actor, "declined")
            case "toggle_private":
                opening = actor in model.private
                response = await patch_private(self.client, user, not opening)
                status = response.status_code
                assert status == 200, (response.text, self.context())
                if opening:
                    model.private.discard(actor)
                    # Открытие одобряет все ждущие запросы по возрастанию просившего, и неактивных тоже.
                    for ask_id in model.waiting_asks_of(actor):
                        ask = model.asks[ask_id]
                        ask.status = "approved"
                        model.follows.add((ask.follower, actor))
                        model.log("FollowRequestResponded", actor, ask.follower, actor, "approved")
                else:
                    model.private.add(actor)
            case _:
                raise AssertionError(operation)
        self.outcomes[(operation, status)] += 1

    async def pages(self, viewer: SignedInUser, address: str) -> tuple[int, list[dict[str, Any]]]:
        """Все страницы списка по две записи: статус первого ответа и записи (без повторов)."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self.client.get(address, params=params, headers=viewer.headers)
            if response.status_code != 200:
                return response.status_code, []
            body = response.json()
            found.extend(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                return 200, found

    async def check(self, *people: SignedInUser, lists: bool = False) -> None:
        """Всё, что видят эти люди, совпадает с моделью: списки, заявки, блокировки, отношения."""
        model = self.model
        for user in people:
            person = user.user_id
            assert person not in model.inactive
            friends = await friend_ids(self.client, user)
            assert len(friends) == len(set(friends)), self.context()
            assert set(friends) == model.friends_of(person), self.context()
            assert set(await blocked_ids(self.client, user)) == model.blocked_by(person), (
                self.context()
            )
            for direction in ("incoming", "outgoing"):
                listed = await request_ids(self.client, user, direction)
                assert len(listed) == len(set(listed)), self.context()
                assert set(listed) == model.pending_ids(person, direction), (
                    direction,
                    self.context(),
                )
            following = await following_ids(self.client, user)
            assert len(following) == len(set(following)), self.context()
            assert set(following) == model.following_of(person), self.context()
            followers = await follower_ids(self.client, user)
            assert len(followers) == len(set(followers)), self.context()
            assert set(followers) == model.followers_of(person), self.context()
            waiting = await follow_request_ids(self.client, user)
            assert len(waiting) == len(set(waiting)), self.context()
            assert set(waiting) == model.visible_waiting_asks(person), self.context()
            for other in self.order:
                if other.user_id == person:
                    continue
                response = await view(self.client, user, other.user_id)
                expected = model.relation(person, other.user_id)
                if expected is None:
                    assert response.status_code == 404, self.context()
                else:
                    assert response.status_code == 200, (response.text, self.context())
                    body = response.json()
                    relationship = body["relationship"]
                    assert relationship["friendship"] == expected, self.context()
                    assert relationship["blocked"] is False
                    has_request = expected in ("request_sent", "request_received")
                    assert (relationship["friend_request_id"] is not None) == has_request
                    assert relationship["following"] == model.following_state(
                        person, other.user_id
                    ), self.context()
                    assert relationship["follows_you"] is (
                        (other.user_id, person) in model.follows
                    ), self.context()
                    # Закрытый профиль прячет подробности от тех, кто не друг и не подписчик.
                    shown = CITY if model.sees_details(person, other.user_id) else None
                    assert body["city"] == shown, self.context()
                    assert body["counters"]["followers"] == len(model.followers_of(other.user_id))
                    assert body["counters"]["following"] == len(model.following_of(other.user_id))
                if lists:
                    await self.check_lists_of(user, other)

    async def check_lists_of(self, viewer: SignedInUser, owner: SignedInUser) -> None:
        """Друзья, общие друзья, подписчики и подписки чужого профиля глазами зрителя."""
        model = self.model
        seen, mine = viewer.user_id, owner.user_id
        hidden = model.hidden_from(seen, mine)
        status, items = await self.pages(viewer, f"{url(mine)}/friends")
        wanted = model.list_status(seen, mine)  # закрытый профиль закрывает и список друзей (403)
        assert status == wanted, (status, wanted, self.context())
        if wanted == 200:
            ids = [item["id"] for item in items]
            assert len(ids) == len(set(ids)), self.context()
            expected = {
                friend for friend in model.friends_of(mine) if not model.blocked(seen, friend)
            }
            assert set(ids) == expected, self.context()
            for item in items:
                relationship = item["relationship"]
                if item["id"] == seen:
                    assert relationship["is_self"] is True
                    assert relationship["friendship"] == "none"
                else:
                    assert relationship["friendship"] == model.relation(seen, item["id"]), (
                        item["id"],
                        self.context(),
                    )
        status, items = await self.pages(viewer, f"{url(mine)}/mutual-friends")
        if hidden:
            assert status == 404, self.context()
        else:
            assert status == 200, self.context()
            ids = [item["id"] for item in items]
            assert len(ids) == len(set(ids)), self.context()
            assert set(ids) == model.friends_of(seen) & model.friends_of(mine), self.context()
        for kind in ("followers", "following"):
            status, items = await self.pages(viewer, f"{url(mine)}/{kind}")
            wanted = model.list_status(seen, mine)
            assert status == wanted, (kind, status, wanted, self.context())
            if wanted != 200:
                continue
            ids = [item["id"] for item in items]
            assert len(ids) == len(set(ids)), self.context()
            base = model.followers_of(mine) if kind == "followers" else model.following_of(mine)
            assert set(ids) == {person for person in base if not model.blocked(seen, person)}, (
                kind,
                self.context(),
            )
            for item in items:
                relationship = item["relationship"]
                if item["id"] == seen:
                    assert relationship["is_self"] is True
                    continue
                assert relationship["following"] == model.following_state(seen, item["id"]), (
                    kind,
                    item["id"],
                    self.context(),
                )
                assert relationship["follows_you"] is ((item["id"], seen) in model.follows)

    async def check_counters(self) -> None:
        for user in self.active():
            own = (await view(self.client, user, user.user_id)).json()
            assert own["counters"]["friends"] == len(self.model.friends_of(user.user_id)), (
                self.context()
            )
            assert own["counters"]["followers"] == len(self.model.followers_of(user.user_id))
            assert own["counters"]["following"] == len(self.model.following_of(user.user_id))
            header = (await self.client.get(ME, headers=user.headers)).json()["counters"]
            waiting = len(self.model.pending_ids(user.user_id, "incoming"))
            assert header["pending_friend_requests"] == waiting, self.context()
            asks = len(self.model.visible_waiting_asks(user.user_id))
            assert header["pending_follow_requests"] == asks, self.context()

    async def check_events(self) -> None:
        """События подписок в outbox совпадают с журналом модели: тип, кто совершил, кто на кого."""
        actual: list[FollowEvent] = [
            (
                row["event_type"],
                row["headers"]["actor_id"],
                row["payload"]["follower_id"],
                row["payload"]["followee_id"],
                row["payload"].get("decision"),
            )
            for row in await graph_events(self.engine)
            if str(row["event_type"]).startswith("Follow")
        ]
        assert actual == self.model.follow_events, self.context()


@pytest.mark.parametrize("seed", [11, 23, 47])
async def test_a_random_walk_over_the_graph_agrees_with_the_model(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    seed: int,
) -> None:
    rng = random.Random(seed)
    people = await users(client, jobs, PEOPLE)
    for person in people:
        await set_privacy(
            client, person, friends_list_visibility="everyone", followers_list_visibility="everyone"
        )
        await fill_profile(client, person, city=CITY)
    walk = Walk(client=client, engine=admin_engine, order=people)
    for closed in people[:2]:  # два закрытых профиля из четырёх: запросам есть куда идти
        await patch_private(client, closed, True)
        walk.model.private.add(closed.user_id)

    for step in range(STEPS):
        operation, actor, other, request_id = walk.choose(rng)
        await walk.apply(operation, actor, other, request_id)
        checked = [user for user in (actor, other) if user.user_id not in walk.model.inactive]
        await walk.check(*checked, lists=step % LISTS_EVERY == 0)

    await walk.check(*walk.active(), lists=True)
    await walk.check_counters()
    await walk.check_events()
    await assert_graph_is_consistent(admin_engine)
    await assert_events_explain_follows(admin_engine)
    # Путь не вырожденный: были и успехи, и отказы разных видов, и уход людей.
    assert len(walk.outcomes) >= 14, walk.outcomes
    assert walk.outcomes[("send", 201)] >= 2, walk.outcomes
    assert walk.outcomes[("follow", 200)] >= 8, walk.outcomes
    assert len(walk.model.follow_events) >= 10, walk.model.follow_events
    kinds = {event[0] for event in walk.model.follow_events}
    assert {"FollowCreated", "FollowRequested", "FollowRemoved"} <= kinds, kinds

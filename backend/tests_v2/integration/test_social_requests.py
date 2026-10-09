"""Заявки в друзья (S7-03, 5.4): отправка, списки, принятие, отказ, отмена, автопринятие встречной."""

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .helpers import ME, PASSWORD, SignedInUser, execute, limited_client, verified_user
from .social_helpers import (
    FRIEND_REQUESTS,
    FRIENDS,
    MY_BLOCKS,
    SUMMARY_KEYS,
    accept,
    block,
    cancel,
    code_of,
    decline,
    friend_ids,
    friendship_rows,
    graph_events,
    request_rows,
    send_request,
    set_status,
    user_uuid,
    users,
)


async def listed(client: httpx.AsyncClient, user: SignedInUser, **query: Any) -> dict[str, Any]:
    response = await client.get(FRIEND_REQUESTS, params=query, headers=user.headers)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


# ----------------------------------------------------------------------------- путь заявки
async def test_a_request_is_sent_listed_by_both_sides_and_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)

    sent = await send_request(client, alice, bob)

    assert sent.status_code == 201, sent.text
    request = sent.json()
    assert set(request) == {"id", "status", "user", "direction", "created_at"}
    assert (request["status"], request["direction"]) == ("pending", "outgoing")
    assert set(request["user"]) == SUMMARY_KEYS
    assert request["user"]["id"] == bob.user_id  # собеседник, не отправитель
    assert request["user"]["username"] == bob.credentials["username"]
    # Получатель видит ту же заявку входящей, со стороны отправителя.
    incoming = (await listed(client, bob))["items"]
    assert [(item["id"], item["direction"], item["user"]["id"]) for item in incoming] == [
        (request["id"], "incoming", alice.user_id)
    ]
    outgoing = (await listed(client, alice, direction="outgoing"))["items"]
    assert [(item["id"], item["direction"]) for item in outgoing] == [(request["id"], "outgoing")]
    assert (await listed(client, alice))["items"] == []  # у отправителя входящих нет

    accepted = await accept(client, bob, request["id"])

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert set(body) == {"friend", "since"}
    assert body["friend"]["id"] == alice.user_id
    assert await friend_ids(client, alice) == [bob.user_id]
    assert await friend_ids(client, bob) == [alice.user_id]
    assert (await listed(client, bob))["items"] == []  # принятая заявка из активных ушла
    assert (await listed(client, alice, direction="outgoing"))["items"] == []
    rows = await request_rows(admin_engine)
    assert [(row["status"], row["responded_at"] is not None) for row in rows] == [
        ("accepted", True)
    ]
    (friendship,) = await friendship_rows(admin_engine)
    assert {friendship["user_low_id"], friendship["user_high_id"]} == {
        uuid.UUID(alice.user_id),
        uuid.UUID(bob.user_id),
    }
    assert friendship["user_low_id"] < friendship["user_high_id"]


async def test_the_events_of_a_request_carry_ids_only_and_the_pair_as_the_partition_key(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    sent = await send_request(client, alice, bob)
    await accept(client, bob, sent.json()["id"])

    events = await graph_events(admin_engine)

    low, high = sorted([alice.user_id, bob.user_id])
    assert [event["event_type"] for event in events] == [
        "FriendRequestSent",
        "FriendRequestResponded",
    ]
    assert {event["key"] for event in events} == {f"{low}:{high}"}
    assert events[0]["payload"] == {
        "request_id": sent.json()["id"],
        "sender_id": alice.user_id,
        "receiver_id": bob.user_id,
    }
    assert events[1]["payload"] == {
        "request_id": sent.json()["id"],
        "sender_id": alice.user_id,
        "receiver_id": bob.user_id,
        "decision": "accepted",
    }
    # Совершивший действие это поле конверта (6.4), а не сообщения: он лежит в заголовках строки.
    assert [event["headers"]["actor_id"] for event in events] == [alice.user_id, bob.user_id]
    for event in events:  # ни имён, ни почты (⚖️)
        assert alice.credentials["email"] not in str(event)
        assert alice.credentials["username"] not in str(event)


async def test_a_declined_request_leaves_the_list_and_may_be_sent_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    first = (await send_request(client, alice, bob)).json()

    declined = await decline(client, bob, first["id"])

    assert declined.status_code == 204
    assert (await listed(client, bob))["items"] == []
    assert await friend_ids(client, alice) == []
    again = await send_request(client, alice, bob)  # отказ не запрещает новую заявку
    assert again.status_code == 201
    assert again.json()["id"] != first["id"]
    events = await graph_events(admin_engine)
    assert [event["event_type"] for event in events] == [
        "FriendRequestSent",
        "FriendRequestResponded",
        "FriendRequestSent",
    ]
    assert events[1]["payload"]["decision"] == "declined"


async def test_the_sender_cancels_a_request_and_no_event_is_written(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    request = (await send_request(client, alice, bob)).json()

    cancelled = await cancel(client, alice, request["id"])

    assert cancelled.status_code == 204
    assert (await listed(client, bob))["items"] == []
    assert [row["status"] for row in await request_rows(admin_engine)] == ["cancelled"]
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FriendRequestSent"]
    assert code_of(await accept(client, bob, request["id"])) == (409, "friend_request_not_pending")


# ----------------------------------------------------------------------------- встречная заявка
async def test_an_opposite_request_is_accepted_at_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    first = (await send_request(client, alice, bob)).json()

    answer = await send_request(client, bob, alice)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "accepted"
    assert body["id"] == first["id"]  # принята та же заявка, новой не появилось
    assert body["direction"] == "incoming"
    assert body["user"]["id"] == alice.user_id
    assert await friend_ids(client, alice) == [bob.user_id]
    assert (await listed(client, alice, direction="outgoing"))["items"] == []
    assert [row["status"] for row in await request_rows(admin_engine)] == ["accepted"]
    events = await graph_events(admin_engine)
    assert [event["event_type"] for event in events] == [
        "FriendRequestSent",
        "FriendRequestResponded",
    ]
    assert events[1]["headers"]["actor_id"] == bob.user_id  # принял тот, кто отправил встречную
    assert events[1]["payload"]["decision"] == "accepted"


# ----------------------------------------------------------------------------- ошибки отправки
async def test_sending_errors_follow_the_specification(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)

    assert code_of(await send_request(client, alice, alice)) == (400, "self_action")
    assert code_of(await send_request(client, alice, str(uuid.uuid4()))) == (404, "not_found")
    assert (await send_request(client, alice, bob)).status_code == 201
    assert code_of(await send_request(client, alice, bob)) == (409, "friend_request_exists")
    # Тот, кому адресована заявка, не может «повторить» её: у него встречная заявка принимается.
    assert (await send_request(client, bob, alice)).status_code == 200
    assert code_of(await send_request(client, alice, bob)) == (409, "already_friends")
    assert code_of(await send_request(client, bob, alice)) == (409, "already_friends")
    assert (await send_request(client, alice, carol)).status_code == 201  # другие люди не затронуты


@pytest.mark.parametrize("status", ["suspended", "banned", "deletion_pending", "pending"])
async def test_a_person_who_is_not_active_cannot_be_asked(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, status: str
) -> None:
    alice, bob = await users(client, jobs, 2)
    await set_status(admin_engine, bob, status)

    response = await send_request(client, alice, bob)

    assert code_of(response) == (404, "not_found")
    assert await request_rows(admin_engine) == []


async def test_a_block_in_either_direction_hides_the_person(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    assert (await block(client, alice, bob)).status_code == 204
    assert (await block(client, carol, alice)).status_code == 204

    assert code_of(await send_request(client, alice, bob)) == (404, "not_found")  # я заблокировал
    assert code_of(await send_request(client, bob, alice)) == (
        404,
        "not_found",
    )  # меня заблокировали
    assert code_of(await send_request(client, alice, carol)) == (404, "not_found")
    assert code_of(await send_request(client, carol, alice)) == (404, "not_found")


async def test_the_request_body_is_validated(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    (alice,) = await users(client, jobs, 1)
    headers = alice.headers

    for body in ({}, {"user_id": "not-a-uuid"}, {"user_id": str(uuid.uuid4()), "extra": 1}):
        response = await client.post(FRIEND_REQUESTS, json=body, headers=headers)
        assert code_of(response) == (422, "validation_error"), body


async def test_requests_need_a_token(client: httpx.AsyncClient) -> None:
    assert (
        await client.post(FRIEND_REQUESTS, json={"user_id": str(uuid.uuid4())})
    ).status_code == 401
    assert (await client.get(FRIEND_REQUESTS)).status_code == 401


# ----------------------------------------------------------------------------- ответы на заявку: права и состояния
async def test_only_the_addressee_may_accept_or_decline_and_only_the_sender_may_cancel(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    request_id = (await send_request(client, alice, bob)).json()["id"]

    # Чужая, своя исходящая и несуществующая заявки одинаково «не найдены».
    assert code_of(await accept(client, carol, request_id)) == (404, "not_found")
    assert code_of(await accept(client, alice, request_id)) == (404, "not_found")
    assert code_of(await decline(client, carol, request_id)) == (404, "not_found")
    assert code_of(await decline(client, alice, request_id)) == (404, "not_found")
    assert code_of(await cancel(client, bob, request_id)) == (404, "not_found")
    assert code_of(await cancel(client, carol, request_id)) == (404, "not_found")
    assert code_of(await accept(client, bob, str(uuid.uuid4()))) == (404, "not_found")
    assert (await accept(client, bob, request_id)).status_code == 200  # всё это ничего не испортило


async def test_a_request_answers_only_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    request_id = (await send_request(client, alice, bob)).json()["id"]
    assert (await accept(client, bob, request_id)).status_code == 200

    assert code_of(await accept(client, bob, request_id)) == (409, "friend_request_not_pending")
    assert code_of(await decline(client, bob, request_id)) == (409, "friend_request_not_pending")
    assert code_of(await cancel(client, alice, request_id)) == (409, "friend_request_not_pending")


async def test_a_request_of_a_person_who_left_is_gone_for_the_addressee(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    request_id = (await send_request(client, alice, bob)).json()["id"]
    await set_status(admin_engine, alice, "deletion_pending")

    assert (await listed(client, bob))["items"] == []  # в списке её уже нет
    assert code_of(await accept(client, bob, request_id)) == (404, "not_found")
    assert code_of(await decline(client, bob, request_id)) == (404, "not_found")
    assert await friendship_rows(admin_engine) == []


# ----------------------------------------------------------------------------- список заявок
async def test_requests_are_listed_newest_first_page_by_page(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    receiver = await verified_user(client, jobs)
    senders = await users(client, jobs, 5)
    sent_ids = [(await send_request(client, sender, receiver)).json()["id"] for sender in senders]

    first = await listed(client, receiver, limit=2)
    assert [item["id"] for item in first["items"]] == sent_ids[::-1][:2]
    assert first["next_cursor"] is not None
    second = await listed(client, receiver, limit=2, cursor=first["next_cursor"])
    third = await listed(client, receiver, limit=2, cursor=second["next_cursor"])

    walked = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert walked == sent_ids[::-1]
    assert third["next_cursor"] is None


async def test_list_parameters_are_validated(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    (alice,) = await users(client, jobs, 1)
    bad = await client.get(FRIEND_REQUESTS, params={"direction": "sideways"}, headers=alice.headers)
    assert code_of(bad) == (422, "validation_error")
    broken = await client.get(FRIEND_REQUESTS, params={"cursor": "abc"}, headers=alice.headers)
    assert code_of(broken) == (400, "invalid_cursor")


# ----------------------------------------------------------------------------- Idempotency-Key и лимит
async def test_a_repeated_request_with_the_same_key_gets_the_stored_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    key = {"Idempotency-Key": str(uuid.uuid4())}

    first = await send_request(client, alice, bob, **key)
    second = await send_request(client, alice, bob, **key)

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotency-replayed"] == "true"
    assert len(await request_rows(admin_engine)) == 1
    assert [e["event_type"] for e in await graph_events(admin_engine)] == ["FriendRequestSent"]


async def test_the_key_is_not_reusable_for_another_person(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert (await send_request(client, alice, bob, **key)).status_code == 201

    other = await send_request(client, alice, carol, **key)

    assert code_of(other) == (422, "idempotency_key_reuse")


async def test_requests_are_limited_per_person(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, friend_request=2) as (_, http):
        alice, *others = await users(http, jobs, 4)

        statuses = [(await send_request(http, alice, other)).status_code for other in others]

        assert statuses == [201, 201, 429]
        limited = await send_request(http, alice, others[2])
        assert limited.json()["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) > 0


async def test_an_account_waiting_for_deletion_cannot_use_the_graph_but_can_be_restored(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)
    requested = await client.request(
        "DELETE", ME, json={"password": PASSWORD}, headers=alice.headers
    )
    assert requested.status_code == 202, requested.text

    refused = [
        await send_request(client, alice, bob),
        await client.get(FRIENDS, headers=alice.headers),
        await client.get(FRIEND_REQUESTS, headers=alice.headers),
        await block(client, alice, bob),
    ]

    assert [code_of(response) for response in refused] == [(403, "account_deletion_pending")] * 4
    # Для других такой человек исчез: заявка ему тоже `404`.
    assert code_of(await send_request(client, bob, alice)) == (404, "not_found")
    restored = await client.post(f"{ME}/restore", headers=alice.headers)
    assert restored.status_code == 200, restored.text
    assert (await send_request(client, bob, alice)).status_code == 201


async def test_counters_and_lists_ignore_requests_from_and_to_people_who_left(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    me, asker, wanted = await users(client, jobs, 3)
    assert (await send_request(client, asker, me)).status_code == 201  # входящая
    assert (await send_request(client, me, wanted)).status_code == 201  # исходящая

    async def waiting() -> int:
        header = (await client.get(ME, headers=me.headers)).json()["counters"]
        return int(header["pending_friend_requests"])

    assert await waiting() == 1
    assert len((await listed(client, me))["items"]) == 1
    assert len((await listed(client, me, direction="outgoing"))["items"]) == 1

    await set_status(admin_engine, asker, "suspended")
    await set_status(admin_engine, wanted, "deletion_pending")
    assert await waiting() == 0
    assert (await listed(client, me))["items"] == []
    assert (await listed(client, me, direction="outgoing"))["items"] == []

    await set_status(admin_engine, asker, "active")  # вернулся: заявка снова на месте
    assert await waiting() == 1
    assert len((await listed(client, me))["items"]) == 1


async def test_requests_with_one_creation_time_are_listed_without_losses_or_repeats(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    receiver, *senders = await users(client, jobs, 6)
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for sender in senders:
        await execute(
            admin_engine,
            "INSERT INTO social.friend_requests (sender_id, receiver_id, created_at) "
            "VALUES (:sender, :receiver, :moment)",
            sender=user_uuid(sender),
            receiver=user_uuid(receiver),
            moment=moment,
        )
    stored = sorted((str(row["id"]) for row in await request_rows(admin_engine)), reverse=True)

    walked: list[str] = []
    cursor: str | None = None
    while True:
        page = await listed(client, receiver, limit=2, **({"cursor": cursor} if cursor else {}))
        walked.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert walked == stored


def forged_cursor(payload: Any) -> str:
    raw = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def renamed(shape: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Ключ страницы под имя поля списка; `{id}` заменяется настоящим идентификатором."""
    return {
        (field_name if name == "key" else name): (str(uuid.uuid4()) if value == "{id}" else value)
        for name, value in shape.items()
    }


@pytest.mark.parametrize(
    ("kind", "shape", "expected"),
    [
        # Разбираются и работают как обычный ключ страницы: подделка не ломает список.
        ("naive-time", {"v": 1, "key": "2026-01-01T00:00:00", "id": "{id}"}, 200),
        ("offset-time", {"v": 1, "key": "2026-01-01T00:00:00+05:00", "id": "{id}"}, 200),
        ("far-future", {"v": 1, "key": "9999-12-31T23:59:59.999999+00:00", "id": "{id}"}, 200),
        ("far-past", {"v": 1, "key": "0001-01-01T00:00:00+00:00", "id": "{id}"}, 200),
        ("epoch-number", {"v": 1, "key": 5, "id": "{id}"}, 200),
        ("extra-field", {"v": 1, "key": "2026-01-01T00:00:00+00:00", "id": "{id}", "x": 1}, 200),
        # Время, которое драйвер не переведёт в UTC (год 0 и год 10000), или не курсор вовсе:
        # `400 invalid_cursor` (находка ревью S8: раньше первое давало 500).
        ("edge-time-east", {"v": 1, "key": "0001-01-01T00:00:00+01:00", "id": "{id}"}, 400),
        ("edge-time-west", {"v": 1, "key": "9999-12-31T23:59:59-01:00", "id": "{id}"}, 400),
        ("list", [1, 2], 400),
        ("null", None, 400),
        ("not-json", "not json at all", 400),
        ("bad-id", {"v": 1, "key": "2026-01-01T00:00:00+00:00", "id": "x"}, 400),
        ("bad-version", {"v": 2, "key": "2026-01-01T00:00:00+00:00", "id": "{id}"}, 400),
        ("missing-id", {"v": 1, "key": "2026-01-01T00:00:00+00:00"}, 400),
    ],
)
async def test_a_forged_cursor_never_breaks_a_list(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, kind: str, shape: Any, expected: int
) -> None:
    (alice,) = await users(client, jobs, 1)
    fields = {FRIENDS: "since", MY_BLOCKS: "blocked_at", FRIEND_REQUESTS: "created_at"}
    for address, field_name in fields.items():
        payload = (
            renamed(cast(dict[str, Any], shape), field_name) if isinstance(shape, dict) else shape
        )
        response = await client.get(
            address, params={"cursor": forged_cursor(payload)}, headers=alice.headers
        )
        assert response.status_code == expected, (kind, address, response.text)

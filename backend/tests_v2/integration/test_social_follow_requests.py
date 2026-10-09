"""Закрытые профили и запросы на подписку (S8-02, 5.4, 5.3): запрос, одобрение, отказ, отмена, списки."""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, execute, fill_profile, view
from .social_helpers import (
    FOLLOW_REQUESTS,
    SUMMARY_KEYS,
    approve_follow,
    assert_graph_is_consistent,
    befriend,
    block,
    code_of,
    decline_follow,
    follow,
    follow_privately,
    follow_request_ids,
    follow_request_rows,
    follow_rows,
    follower_ids,
    following_ids,
    graph_events,
    incoming_follow_requests,
    remove_follower_of,
    set_private,
    set_status,
    unfollow,
    user_uuid,
    users,
)


def event_types(rows: list[dict[str, Any]]) -> list[str]:
    return [row["event_type"] for row in rows]


async def pending_counter(client: httpx.AsyncClient, user: Any) -> int:
    header = (await client.get(ME, headers=user.headers)).json()["counters"]
    return int(header["pending_follow_requests"])


# ----------------------------------------------------------------------------- путь запроса
async def test_a_follow_of_a_private_profile_is_a_request_that_the_owner_approves(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await fill_profile(client, owner, is_private=True)

    answer = await follow(client, asker, owner)

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"status": "requested"}
    assert await following_ids(client, asker) == []  # подписки ещё нет
    assert await follower_ids(client, owner) == []
    seen = (await view(client, asker, owner.user_id)).json()
    assert seen["relationship"]["following"] == "requested"
    assert (seen["links"], seen["city"], seen["counters"]["posts"]) == ([], None, None)
    waiting = await incoming_follow_requests(client, owner)
    (request,) = waiting
    assert set(request) == {"id", "user", "created_at"}
    assert set(request["user"]) == SUMMARY_KEYS
    assert request["user"]["id"] == asker.user_id
    assert await pending_counter(client, owner) == 1

    approved = await approve_follow(client, owner, request["id"])

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert set(body) == {"follower"}
    assert set(body["follower"]) == SUMMARY_KEYS
    assert body["follower"]["id"] == asker.user_id
    assert await follower_ids(client, owner) == [asker.user_id]
    assert await following_ids(client, asker) == [owner.user_id]
    assert await incoming_follow_requests(client, owner) == []
    assert await pending_counter(client, owner) == 0
    seen = (await view(client, asker, owner.user_id)).json()
    assert seen["relationship"]["following"] == "following"  # подписчик закрытого профиля
    assert seen["city"] == "Казань"
    assert seen["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert seen["counters"]["posts"] == 0
    # Хозяин видит, что asker на него подписан.
    mine = (await view(client, owner, asker.user_id)).json()["relationship"]
    assert (mine["following"], mine["follows_you"]) == ("none", True)
    # Строки: запрос одобрен и время ответа совпадает со временем подписки.
    (stored,) = await follow_request_rows(admin_engine)
    assert stored["status"] == "approved"
    (follow_row,) = await follow_rows(admin_engine)
    assert follow_row["created_at"] == stored["responded_at"]
    await assert_graph_is_consistent(admin_engine)


async def test_the_events_of_a_request_and_its_approval_carry_ids_only(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)
    await approve_follow(client, owner, request_id)

    events = await graph_events(admin_engine)

    low, high = sorted([owner.user_id, asker.user_id])
    # Одобрение само создаёт подписку: отдельного `FollowCreated` нет.
    assert event_types(events) == ["FollowRequested", "FollowRequestResponded"]
    assert {event["key"] for event in events} == {f"{low}:{high}"}
    ids = {"request_id": request_id, "follower_id": asker.user_id, "followee_id": owner.user_id}
    assert events[0]["payload"] == ids
    assert events[1]["payload"] == {**ids, "decision": "approved"}
    assert [event["headers"]["actor_id"] for event in events] == [asker.user_id, owner.user_id]
    for event in events:  # ни имён, ни почты (⚖️)
        assert asker.credentials["email"] not in str(event)
        assert owner.credentials["username"] not in str(event)


async def test_the_owner_declines_a_request_and_the_asker_may_ask_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (first,) = await follow_request_ids(client, owner)

    declined = await decline_follow(client, owner, first)

    assert declined.status_code == 204
    assert declined.content == b""
    assert await incoming_follow_requests(client, owner) == []
    assert await follower_ids(client, owner) == []
    seen = (await view(client, asker, owner.user_id)).json()["relationship"]
    assert (
        seen["following"] == "none"
    )  # об отказе просивший узнаёт лишь тем, что запроса больше нет
    again = await follow(client, asker, owner)  # отказ не запрещает новый запрос
    assert again.json() == {"status": "requested"}
    (second,) = await follow_request_ids(client, owner)
    assert second != first
    events = await graph_events(admin_engine)
    assert event_types(events) == ["FollowRequested", "FollowRequestResponded", "FollowRequested"]
    assert events[1]["payload"]["decision"] == "declined"
    assert events[1]["headers"]["actor_id"] == owner.user_id
    statuses = [row["status"] for row in await follow_request_rows(admin_engine)]
    assert statuses == ["declined", "pending"]  # история копится
    await assert_graph_is_consistent(admin_engine)


async def test_the_asker_cancels_a_waiting_request_with_a_delete_and_no_event_is_written(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)

    cancelled = await unfollow(client, asker, owner)

    assert cancelled.status_code == 204
    assert await incoming_follow_requests(client, owner) == []
    (stored,) = await follow_request_rows(admin_engine)
    assert (stored["status"], stored["responded_at"] is not None) == ("cancelled", True)
    assert event_types(await graph_events(admin_engine)) == [
        "FollowRequested"
    ]  # отмена без события
    assert code_of(await approve_follow(client, owner, request_id)) == (
        409,
        "follow_request_not_pending",
    )
    seen = (await view(client, asker, owner.user_id)).json()["relationship"]
    assert seen["following"] == "none"
    assert (await follow(client, asker, owner)).json() == {"status": "requested"}  # снова можно


async def test_a_repeated_follow_of_a_private_profile_keeps_one_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)

    answers = [await follow(client, asker, owner) for _ in range(3)]

    assert [(a.status_code, a.json()) for a in answers] == [(200, {"status": "requested"})] * 3
    assert len(await follow_request_rows(admin_engine)) == 1
    assert event_types(await graph_events(admin_engine)) == ["FollowRequested"]


async def test_friendship_does_not_skip_the_request_of_a_private_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """4.6: закрытый профиль всегда через запрос, даже для друга."""
    owner, friend = await users(client, jobs, 2)
    await befriend(client, owner, friend)
    await set_private(client, owner, True)

    answer = await follow(client, friend, owner)

    assert answer.json() == {"status": "requested"}
    assert await follow_rows(admin_engine) == []


async def test_a_declined_and_a_cancelled_request_stay_as_history_and_do_not_block_new_ones(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    await decline_follow(client, owner, (await follow_request_ids(client, owner))[0])
    await follow(client, asker, owner)
    await unfollow(client, asker, owner)

    await follow_privately(client, asker, owner)

    statuses = [row["status"] for row in await follow_request_rows(admin_engine)]
    assert statuses == ["declined", "cancelled", "approved"]
    assert len(await follow_rows(admin_engine)) == 1
    await assert_graph_is_consistent(admin_engine)


async def test_unfollowing_after_the_approval_removes_the_follow_and_keeps_the_request_closed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow_privately(client, asker, owner)

    assert (await unfollow(client, asker, owner)).status_code == 204

    assert await follow_rows(admin_engine) == []
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["approved"]
    assert event_types(await graph_events(admin_engine)) == [
        "FollowRequested",
        "FollowRequestResponded",
        "FollowRemoved",
    ]
    # Закрытый профиль: чтобы вернуться, нужен новый запрос.
    assert (await follow(client, asker, owner)).json() == {"status": "requested"}


async def test_removing_a_follower_of_a_private_profile_requires_a_new_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, asker, waiting = await users(client, jobs, 3)
    await set_private(client, owner, True)
    await follow_privately(client, asker, owner)
    await follow(client, waiting, owner)

    assert (await remove_follower_of(client, owner, asker)).status_code == 204
    assert (await remove_follower_of(client, owner, waiting)).status_code == 204

    assert await follower_ids(client, owner) == []
    # Ждущий запрос не подписка: удаление подписчика его не отменяет, ответить на него можно.
    waiting_now = await incoming_follow_requests(client, owner)
    assert [item["user"]["id"] for item in waiting_now] == [waiting.user_id]
    assert (await follow(client, asker, owner)).json() == {"status": "requested"}


# ----------------------------------------------------------------------------- кто и как отвечает
async def test_only_the_owner_answers_a_request_and_others_get_not_found(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker, stranger = await users(client, jobs, 3)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)

    # Чужой, сам просивший и несуществующий запросы одинаково «не найдены».
    for who in (stranger, asker):
        assert code_of(await approve_follow(client, who, request_id)) == (404, "not_found")
        assert code_of(await decline_follow(client, who, request_id)) == (404, "not_found")
    assert code_of(await approve_follow(client, owner, str(uuid.uuid4()))) == (404, "not_found")
    assert code_of(await decline_follow(client, owner, str(uuid.uuid4()))) == (404, "not_found")
    assert code_of(await approve_follow(client, owner, "not-a-uuid")) == (422, "validation_error")
    # Ничего из этого запрос не испортило.
    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["pending"]
    assert (await approve_follow(client, owner, request_id)).status_code == 200


async def test_a_request_answers_only_once(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, approved_asker, declined_asker = await users(client, jobs, 3)
    await set_private(client, owner, True)
    approved_id = await follow_privately(client, approved_asker, owner)
    await follow(client, declined_asker, owner)
    declined_id = next(
        item["id"]
        for item in await incoming_follow_requests(client, owner)
        if item["user"]["id"] == declined_asker.user_id
    )
    assert (await decline_follow(client, owner, declined_id)).status_code == 204
    before = len(await graph_events(admin_engine))

    for request_id in (approved_id, declined_id):
        assert code_of(await approve_follow(client, owner, request_id)) == (
            409,
            "follow_request_not_pending",
        )
        assert code_of(await decline_follow(client, owner, request_id)) == (
            409,
            "follow_request_not_pending",
        )

    assert (
        len(await graph_events(admin_engine)) == before
    )  # отказ повторного ответа событий не пишет
    assert await follower_ids(client, owner) == [approved_asker.user_id]


async def test_a_request_of_a_person_who_left_is_gone_for_the_owner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)
    (request_id,) = await follow_request_ids(client, owner)
    await set_status(admin_engine, asker, "deletion_pending")

    assert await incoming_follow_requests(client, owner) == []  # в списке его уже нет
    assert await pending_counter(client, owner) == 0  # и в счётчике шапки
    assert code_of(await approve_follow(client, owner, request_id)) == (404, "not_found")
    assert code_of(await decline_follow(client, owner, request_id)) == (404, "not_found")
    assert await follow_rows(admin_engine) == []

    await set_status(admin_engine, asker, "active")  # вернулся: запрос снова на месте
    assert await pending_counter(client, owner) == 1
    assert (await approve_follow(client, owner, request_id)).status_code == 200


# ----------------------------------------------------------------------------- список запросов
async def test_follow_requests_are_listed_newest_first_page_by_page(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, *askers = await users(client, jobs, 6)
    await set_private(client, owner, True)
    for asker in askers:
        await follow(client, asker, owner)

    first = (await client.get(FOLLOW_REQUESTS, params={"limit": 2}, headers=owner.headers)).json()
    assert [item["user"]["id"] for item in first["items"]] == [a.user_id for a in askers[::-1][:2]]
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            FOLLOW_REQUESTS,
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=owner.headers,
        )
    ).json()
    third = (
        await client.get(
            FOLLOW_REQUESTS,
            params={"limit": 2, "cursor": second["next_cursor"]},
            headers=owner.headers,
        )
    ).json()

    walked = [item["user"]["id"] for page in (first, second, third) for item in page["items"]]
    assert walked == [asker.user_id for asker in askers[::-1]]
    assert (len(second["items"]), len(third["items"])) == (2, 1)
    assert third["next_cursor"] is None
    stamps = [item["created_at"] for page in (first, second, third) for item in page["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert await pending_counter(client, owner) == 5


async def test_requests_with_one_creation_time_are_listed_without_losses_or_repeats(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, *askers = await users(client, jobs, 6)
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for asker in askers:
        await execute(
            admin_engine,
            "INSERT INTO social.follow_requests (follower_id, followee_id, created_at) "
            "VALUES (:follower, :followee, :moment)",
            follower=user_uuid(asker),
            followee=user_uuid(owner),
            moment=moment,
        )
    stored = sorted(
        (str(row["id"]) for row in await follow_request_rows(admin_engine)), reverse=True
    )

    walked: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(FOLLOW_REQUESTS, params=params, headers=owner.headers)).json()
        walked.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert walked == stored


async def test_a_person_sees_only_requests_addressed_to_them(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    first_owner, second_owner, asker = await users(client, jobs, 3)
    await set_private(client, first_owner, True)
    await set_private(client, second_owner, True)
    await follow(client, asker, first_owner)
    await follow(client, asker, second_owner)

    assert [i["user"]["id"] for i in await incoming_follow_requests(client, first_owner)] == [
        asker.user_id
    ]
    # Просивший своих исходящих запросов в этом списке не видит: он показывает только входящие.
    assert await incoming_follow_requests(client, asker) == []
    assert await pending_counter(client, asker) == 0


# ----------------------------------------------------------------------------- защита от неожиданных состояний
async def test_a_waiting_request_on_an_open_profile_stays_a_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Так быть не должно (открытие профиля одобряет ждущие запросы), но если случилось, подписка
    не удваивается, а ответ остаётся честным: запрос ждёт."""
    owner, asker = await users(client, jobs, 2)
    await execute(
        admin_engine,
        "INSERT INTO social.follow_requests (follower_id, followee_id) VALUES (:a, :b)",
        a=user_uuid(asker),
        b=user_uuid(owner),
    )

    answer = await follow(client, asker, owner)

    assert answer.json() == {"status": "requested"}
    assert await follow_rows(admin_engine) == []
    assert await graph_events(admin_engine) == []


async def test_a_block_beats_an_existing_follow_in_the_answer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Подписка вместе с блокировкой невозможна для команд, но порядок проверок политики таков:
    скрытый человек сильнее готовой подписки, и ответ ничего не раскрывает."""
    alice, bob = await users(client, jobs, 2)
    await execute(
        admin_engine,
        "INSERT INTO social.follows (follower_id, followee_id) VALUES (:a, :b)",
        a=user_uuid(alice),
        b=user_uuid(bob),
    )
    await execute(
        admin_engine,
        "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
        a=user_uuid(bob),
        b=user_uuid(alice),
    )

    answer = await follow(client, alice, bob)

    assert code_of(answer) == (404, "not_found")


async def test_blocking_a_person_with_a_waiting_request_cancels_it(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, asker = await users(client, jobs, 2)
    await set_private(client, owner, True)
    await follow(client, asker, owner)

    assert (await block(client, owner, asker)).status_code == 204

    assert [row["status"] for row in await follow_request_rows(admin_engine)] == ["cancelled"]
    assert await incoming_follow_requests(client, owner) == []
    assert event_types(await graph_events(admin_engine)) == ["FollowRequested", "UserBlocked"]
    await assert_graph_is_consistent(admin_engine)

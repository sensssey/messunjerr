"""Друзья (S7-04, 5.4, 5.3): свой список с поиском, удаление, чужие списки и общие друзья, счётчики."""

import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue

from .helpers import ME, SignedInUser, execute, fill_profile, set_privacy, url, verified_user, view
from .social_helpers import (
    FRIEND_REQUESTS,
    FRIENDS,
    SUMMARY_KEYS,
    accept,
    befriend,
    block,
    block_rows,
    code_of,
    friend_ids,
    friendship_rows,
    graph_events,
    request_rows,
    send_request,
    set_status,
    user_uuid,
    users,
)


async def friends_of(
    client: httpx.AsyncClient, viewer: SignedInUser, ref: str, **query: Any
) -> httpx.Response:
    return await client.get(f"{url(ref)}/friends", params=query, headers=viewer.headers)


async def mutual_with(
    client: httpx.AsyncClient, viewer: SignedInUser, ref: str, **query: Any
) -> httpx.Response:
    return await client.get(f"{url(ref)}/mutual-friends", params=query, headers=viewer.headers)


# ----------------------------------------------------------------------------- свой список
async def test_my_friends_are_listed_newest_first_with_the_date_of_friendship(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    me, first, second, third = await users(client, jobs, 4)
    for friend in (first, second, third):
        await befriend(client, me, friend)

    page = (await client.get(FRIENDS, headers=me.headers)).json()

    assert [item["user"]["id"] for item in page["items"]] == [
        third.user_id,
        second.user_id,
        first.user_id,
    ]
    assert page["next_cursor"] is None
    for item in page["items"]:
        assert set(item) == {"user", "since"}
        assert set(item["user"]) == SUMMARY_KEYS
    sinces = [item["since"] for item in page["items"]]
    assert sinces == sorted(sinces, reverse=True)


async def test_my_friends_page_by_page(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> None:
    me, *friends = await users(client, jobs, 6)
    for friend in friends:
        await befriend(client, me, friend)

    first = (await client.get(FRIENDS, params={"limit": 2}, headers=me.headers)).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            FRIENDS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=me.headers
        )
    ).json()

    assert {item["user"]["id"] for item in first["items"]}.isdisjoint(
        item["user"]["id"] for item in second["items"]
    )
    assert sorted(await friend_ids(client, me)) == sorted(friend.user_id for friend in friends)


async def test_my_friends_are_searched_by_name_or_username_without_wildcards(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    suffix = uuid.uuid4().hex[:10]
    me = await verified_user(client, jobs)
    # В никах Анны и Бориса нет `_`, у Кэрол есть: так видно, что `_` не шаблон «любой символ».
    anna = await verified_user(client, jobs, username=f"anna{suffix}")
    boris = await verified_user(client, jobs, username=f"boris{suffix}")
    carol = await verified_user(client, jobs, username=f"carol_{suffix}")
    await fill_profile(client, anna, display_name="Анна Каренина")
    await fill_profile(client, boris, display_name="100% Борис")
    await fill_profile(client, carol, display_name="Кэрол")
    everyone = [anna.user_id, boris.user_id, carol.user_id]
    for friend in (anna, boris, carol):
        await befriend(client, me, friend)

    assert await friend_ids(client, me, q="ANNA") == [anna.user_id]  # ник, регистр не важен
    assert await friend_ids(client, me, q="каренин") == [anna.user_id]  # имя
    assert await friend_ids(client, me, q=suffix) == everyone[::-1]  # подстрока ника, новые сверху
    # `%` и `_` ищутся как символы, а не как шаблон «что угодно».
    assert await friend_ids(client, me, q="%") == [boris.user_id]
    assert await friend_ids(client, me, q="_") == [carol.user_id]
    assert await friend_ids(client, me, q="\\") == []
    assert await friend_ids(client, me, q="нет такого") == []
    assert sorted(await friend_ids(client, me, q="   ")) == sorted(
        everyone
    )  # пробелы это весь список


async def test_my_friends_hide_people_who_are_not_active(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    me, kept, gone = await users(client, jobs, 3)
    await befriend(client, me, kept)
    await befriend(client, me, gone)
    await set_status(admin_engine, gone, "deletion_pending")

    assert await friend_ids(client, me) == [kept.user_id]
    counters = (await client.get(url(me.user_id), headers=me.headers)).json()["counters"]
    assert counters["friends"] == 1  # счётчик совпадает со списком


# ----------------------------------------------------------------------------- удаление из друзей
async def test_a_friend_is_removed_for_both_sides_and_the_event_is_written(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob = await users(client, jobs, 2)
    await befriend(client, alice, bob)

    removed = await client.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers)

    assert removed.status_code == 204
    assert await friend_ids(client, alice) == []
    assert await friend_ids(client, bob) == []
    assert await friendship_rows(admin_engine) == []
    event = (await graph_events(admin_engine))[-1]
    low, high = sorted([alice.user_id, bob.user_id])
    assert event["event_type"] == "FriendshipRemoved"
    assert event["key"] == f"{low}:{high}"
    assert event["payload"] == {"user_low_id": low, "user_high_id": high}
    assert event["headers"]["actor_id"] == alice.user_id
    # После удаления можно снова подружиться (заявка новая).
    await befriend(client, bob, alice)
    assert await friend_ids(client, alice) == [bob.user_id]


async def test_removing_someone_who_is_not_a_friend_is_not_found(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await befriend(client, alice, bob)

    assert code_of(await client.delete(f"{FRIENDS}/{carol.user_id}", headers=alice.headers)) == (
        404,
        "not_found",
    )
    assert code_of(await client.delete(f"{FRIENDS}/{uuid.uuid4()}", headers=alice.headers)) == (
        404,
        "not_found",
    )
    assert code_of(await client.delete(f"{FRIENDS}/{alice.user_id}", headers=alice.headers)) == (
        404,
        "not_found",
    )
    assert (
        await client.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers)
    ).status_code == 204
    assert code_of(await client.delete(f"{FRIENDS}/{bob.user_id}", headers=alice.headers)) == (
        404,
        "not_found",
    )  # второй раз уже не друзья


# ----------------------------------------------------------------------------- друзья чужого профиля
async def test_the_friends_of_another_person_carry_my_relationship_to_each(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer, friend, asked, asking, stranger = await users(client, jobs, 6)
    for person in (viewer, friend, asked, asking, stranger):
        await befriend(client, owner, person)
    await befriend(client, viewer, friend)  # с этим человеком зритель дружит сам
    assert (
        await send_request(client, viewer, asked)
    ).status_code == 201  # зритель отправил ему заявку
    assert (
        await send_request(client, asking, viewer)
    ).status_code == 201  # он отправил заявку зрителю
    await set_privacy(client, owner, friends_list_visibility="everyone")

    response = await friends_of(client, viewer, owner.user_id)

    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {
        viewer.user_id,
        friend.user_id,
        asked.user_id,
        asking.user_id,
        stranger.user_id,
    }
    assert set(items[friend.user_id]) == SUMMARY_KEYS | {"relationship"}
    relation = {key: item["relationship"]["friendship"] for key, item in items.items()}
    assert relation == {
        viewer.user_id: "none",  # сам зритель в списке владельца: с собой «дружбы» нет
        friend.user_id: "friends",
        asked.user_id: "request_sent",
        asking.user_id: "request_received",
        stranger.user_id: "none",
    }
    assert items[viewer.user_id]["relationship"]["is_self"] is True
    assert not any(
        item["relationship"]["is_self"] for key, item in items.items() if key != viewer.user_id
    )
    assert items[asked.user_id]["relationship"]["friend_request_id"] is not None
    assert items[friend.user_id]["relationship"]["friend_request_id"] is None
    assert all(item["relationship"]["blocked"] is False for item in items.values())


async def test_the_list_of_a_person_follows_his_setting_and_the_privacy_of_his_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, friend, stranger, someone = await users(client, jobs, 4)
    await befriend(client, owner, friend)
    await befriend(client, owner, someone)

    # По умолчанию друзей видят только друзья.
    assert (await friends_of(client, friend, owner.user_id)).status_code == 200
    assert code_of(await friends_of(client, stranger, owner.user_id)) == (403, "list_hidden")
    # Свой список виден всегда, даже при `only_me`.
    await set_privacy(client, owner, friends_list_visibility="only_me")
    assert (await friends_of(client, owner, owner.user_id)).status_code == 200
    assert code_of(await friends_of(client, friend, owner.user_id)) == (403, "list_hidden")
    # `everyone` открывает список любому, пока профиль открыт.
    await set_privacy(client, owner, friends_list_visibility="everyone")
    assert (await friends_of(client, stranger, owner.user_id)).status_code == 200
    # Закрытый профиль закрывает список от чужих, что бы ни говорила настройка.
    await fill_profile(client, owner, is_private=True)
    assert code_of(await friends_of(client, stranger, owner.user_id)) == (403, "profile_private")
    assert (await friends_of(client, friend, owner.user_id)).status_code == 200  # друг видит


def expected_friends_list(
    kind: str, *, is_private: bool, visibility: str
) -> tuple[int, str | None]:
    """Матрица доступа к списку друзей чужого профиля (4.6) в виде таблицы, а не цепочки `if`."""
    if kind in {"blocked_by_viewer", "blocked_by_owner", "owner_not_active"}:
        return 404, "not_found"
    if kind == "self":
        return 200, None
    details_shown = kind == "friend" or not is_private
    if not details_shown:
        return 403, "profile_private"
    shown_by_setting = visibility == "everyone" or (visibility == "friends" and kind == "friend")
    return (200, None) if shown_by_setting else (403, "list_hidden")


async def test_the_access_matrix_of_a_friends_list_holds_at_the_api_level(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Все виды зрителя против закрытости профиля и настройки списка, через настоящие ручки."""
    owner, friend, stranger, blocked_by_viewer, blocked_by_owner, mutual_friend = await users(
        client, jobs, 6
    )
    await befriend(client, owner, friend)
    await befriend(client, owner, mutual_friend)
    await befriend(client, stranger, mutual_friend)
    assert (await block(client, blocked_by_viewer, owner)).status_code == 204
    assert (await block(client, owner, blocked_by_owner)).status_code == 204
    viewers = {
        "self": owner,
        "friend": friend,
        "stranger": stranger,
        "blocked_by_viewer": blocked_by_viewer,
        "blocked_by_owner": blocked_by_owner,
    }

    for is_private in (False, True):
        for visibility in ("everyone", "friends", "only_me"):
            await fill_profile(client, owner, is_private=is_private)
            await set_privacy(client, owner, friends_list_visibility=visibility)
            for kind, viewer in viewers.items():
                response = await friends_of(client, viewer, owner.user_id)
                expected = expected_friends_list(kind, is_private=is_private, visibility=visibility)
                got = (
                    response.status_code,
                    None if response.is_success else response.json()["code"],
                )
                assert got == expected, (kind, is_private, visibility, response.text)
                # Общие друзья известны зрителю по определению: их закрывает только скрытие человека.
                mutual = await mutual_with(client, viewer, owner.user_id)
                assert mutual.status_code == (404 if expected[1] == "not_found" else 200), (
                    kind,
                    is_private,
                    visibility,
                )

    await set_status(admin_engine, owner, "suspended")
    for kind in ("friend", "stranger"):
        response = await friends_of(client, viewers[kind], owner.user_id)
        assert code_of(response) == (404, "not_found"), kind
        assert code_of(await mutual_with(client, viewers[kind], owner.user_id)) == (
            404,
            "not_found",
        )


async def test_the_list_by_username_and_the_paging_of_a_persons_friends(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, viewer, *friends = await users(client, jobs, 5)
    for friend in friends:
        await befriend(client, owner, friend)
    await set_privacy(client, owner, friends_list_visibility="everyone")
    ref = owner.credentials["username"]

    first = (await friends_of(client, viewer, ref, limit=2)).json()
    second = (await friends_of(client, viewer, ref, limit=2, cursor=first["next_cursor"])).json()

    assert (len(first["items"]), len(second["items"])) == (2, 1)
    assert second["next_cursor"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == {f.user_id for f in friends}


async def test_nobody_is_listed_for_a_hidden_owner_and_hidden_friends_stay_hidden(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner, viewer, shown, blocked_by_viewer, away = await users(client, jobs, 5)
    for friend in (shown, blocked_by_viewer, away):
        await befriend(client, owner, friend)
    await set_privacy(client, owner, friends_list_visibility="everyone")
    assert (await block(client, viewer, blocked_by_viewer)).status_code == 204
    await set_status(admin_engine, away, "suspended")

    listed = (await friends_of(client, viewer, owner.user_id)).json()["items"]

    # Заблокированного зрителем и неактивного в чужом списке нет: для зрителя они не существуют.
    assert [item["id"] for item in listed] == [shown.user_id]
    # Нет такого человека, блокировка и неактивный владелец: `404`, а не пустой список.
    assert code_of(await friends_of(client, viewer, str(uuid.uuid4()))) == (404, "not_found")
    assert (await block(client, owner, viewer)).status_code == 204
    assert code_of(await friends_of(client, viewer, owner.user_id)) == (404, "not_found")
    assert code_of(await mutual_with(client, viewer, owner.user_id)) == (404, "not_found")


# ----------------------------------------------------------------------------- общие друзья
async def test_mutual_friends_are_those_who_are_friends_of_both(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    viewer, owner, both_1, both_2, only_viewer, only_owner = await users(client, jobs, 6)
    for friend in (both_1, both_2, only_viewer):
        await befriend(client, viewer, friend)
    for friend in (both_1, both_2, only_owner):
        await befriend(client, owner, friend)

    response = await mutual_with(client, viewer, owner.user_id)

    assert response.status_code == 200, response.text
    page = response.json()
    assert {item["id"] for item in page["items"]} == {both_1.user_id, both_2.user_id}
    assert all(set(item) == SUMMARY_KEYS for item in page["items"])  # без `relationship`
    assert page["next_cursor"] is None
    # Взгляд с другой стороны даёт тех же людей.
    reverse = (await mutual_with(client, owner, viewer.user_id)).json()["items"]
    assert {item["id"] for item in reverse} == {both_1.user_id, both_2.user_id}
    # Дружба между самими двумя общих друзей не создаёт.
    await befriend(client, viewer, owner)
    again = (await mutual_with(client, viewer, owner.user_id)).json()["items"]
    assert {item["id"] for item in again} == {both_1.user_id, both_2.user_id}


async def test_mutual_friends_page_and_skip_the_inactive_and_myself(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    viewer, owner, *shared = await users(client, jobs, 5)
    for friend in shared:
        await befriend(client, viewer, friend)
        await befriend(client, owner, friend)
    await set_status(admin_engine, shared[0], "banned")

    first = (await mutual_with(client, viewer, owner.user_id, limit=1)).json()
    second = (
        await mutual_with(client, viewer, owner.user_id, limit=1, cursor=first["next_cursor"])
    ).json()

    assert len(first["items"]) == len(second["items"]) == 1
    listed = {item["id"] for item in first["items"] + second["items"]}
    assert listed <= {friend.user_id for friend in shared[1:]}
    # Свой профиль: общих друзей с самим собой нет.
    assert (await mutual_with(client, viewer, viewer.user_id)).json() == {
        "items": [],
        "next_cursor": None,
    }
    assert code_of(await mutual_with(client, viewer, str(uuid.uuid4()))) == (404, "not_found")


# ----------------------------------------------------------------------------- счётчики и отношение в профиле
async def test_the_counters_of_the_profile_and_the_header_follow_the_graph(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner, friend, asker_1, asker_2 = await users(client, jobs, 4)

    assert (await client.get(ME, headers=owner.headers)).json()["counters"][
        "pending_friend_requests"
    ] == 0
    for asker in (asker_1, asker_2):
        await send_request(client, asker, owner)
    header = (await client.get(ME, headers=owner.headers)).json()["counters"]
    assert header["pending_friend_requests"] == 2

    request = (await client.get(FRIEND_REQUESTS, headers=owner.headers)).json()["items"][0]
    assert (await accept(client, owner, request["id"])).status_code == 200
    await befriend(client, friend, owner)

    header = (await client.get(ME, headers=owner.headers)).json()["counters"]
    assert header["pending_friend_requests"] == 1  # одну приняли, вторая ждёт
    seen_by_friend = (await view(client, friend, owner.user_id)).json()
    assert seen_by_friend["counters"]["friends"] == 2


async def test_the_relationship_in_a_profile_follows_the_state_of_the_pair(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice, bob = await users(client, jobs, 2)

    async def relationship() -> dict[str, Any]:
        profile = (await view(client, alice, bob.user_id)).json()
        relation: dict[str, Any] = profile["relationship"]
        return relation

    assert (await relationship())["friendship"] == "none"
    sent = (await send_request(client, alice, bob)).json()
    assert await relationship() == {
        "is_self": False,
        "friendship": "request_sent",
        "friend_request_id": sent["id"],
        "following": "none",
        "follows_you": False,
        "blocked": False,
    }
    await client.delete(f"/api/v1/friend-requests/{sent['id']}", headers=alice.headers)
    received = (await send_request(client, bob, alice)).json()
    assert await relationship() == {
        "is_self": False,
        "friendship": "request_received",
        "friend_request_id": received["id"],
        "following": "none",
        "follows_you": False,
        "blocked": False,
    }
    await client.post(f"/api/v1/friend-requests/{received['id']}/accept", headers=alice.headers)
    assert (await relationship())["friendship"] == "friends"
    assert (await relationship())["friend_request_id"] is None


async def test_a_private_profile_opens_its_details_to_friends_after_the_request_is_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(client, jobs)
    await fill_profile(client, owner, is_private=True)
    viewer = await verified_user(client, jobs)

    before = (await view(client, viewer, owner.user_id)).json()
    assert (before["city"], before["links"]) == (None, [])

    await befriend(client, viewer, owner)

    after = (await view(client, viewer, owner.user_id)).json()
    assert after["city"] == "Казань"
    assert after["links"] == [{"title": "Блог", "url": "https://example.com"}]


async def test_deleting_an_account_cascades_to_its_friendships_and_requests(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    alice, bob, carol = await users(client, jobs, 3)
    await befriend(client, alice, bob)
    await send_request(client, carol, alice)
    await block(client, alice, carol)  # заявка отменена, блокировка осталась

    await execute(admin_engine, "DELETE FROM identity.users WHERE id = :id", id=user_uuid(alice))

    assert await friendship_rows(admin_engine) == []
    assert await request_rows(admin_engine) == []
    assert await block_rows(admin_engine) == []


async def test_the_search_is_normalized_and_control_characters_are_refused(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    me, friend = await users(client, jobs, 2)
    await fill_profile(client, friend, display_name="Йохан Ёлкин")
    await befriend(client, me, friend)

    # «й» в виде «и» с краткой находится как обычная: строка поиска приводится к NFC, как и имена.
    decomposed = unicodedata.normalize("NFD", "йохан")
    assert decomposed != "йохан"
    assert await friend_ids(client, me, q=decomposed) == [friend.user_id]
    assert await friend_ids(client, me, q="  ёлкин  ") == [friend.user_id]  # края обрезаны
    for bad in ("\x00", "a\x00b", "\x07"):  # NUL PostgreSQL в тексте не принимает: раньше это `500`
        refused = await client.get(FRIENDS, params={"q": bad}, headers=me.headers)
        assert code_of(refused) == (422, "validation_error"), repr(bad)
    too_long = await client.get(FRIENDS, params={"q": "x" * 101}, headers=me.headers)
    assert code_of(too_long) == (422, "validation_error")


async def test_equal_dates_of_friendship_neither_lose_nor_repeat_people(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Ключ страницы `(since, id)`: при одинаковом времени порядок задаёт идентификатор друга."""
    me, *others = await users(client, jobs, 6)
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for other in others:
        low, high = sorted([user_uuid(me), user_uuid(other)], key=lambda value: value.int)
        await execute(
            admin_engine,
            "INSERT INTO social.friendships (user_low_id, user_high_id, created_at) "
            "VALUES (:low, :high, :moment)",
            low=low,
            high=high,
            moment=moment,
        )

    walked: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(FRIENDS, params=params, headers=me.headers)).json()
        walked.extend(item["user"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert walked == sorted((other.user_id for other in others), reverse=True)

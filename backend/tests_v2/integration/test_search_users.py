"""Поиск людей `GET /search/users` (S8-04, 5.7, 4.10): порядок выдачи, русские имена, фильтры, страницы.

Люди создаются напрямую в БД с точными никами и именами (`search_helpers`), чтобы порядок выдачи
можно было назвать заранее. Что выбрано и почему (пороги, форма меры), записано в заметках S8.
"""

import asyncio
import random
import re
import unicodedata
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.seeding import big_display_name
from messunjerr.settings import Settings
from messunjerr.social.queries.search import (
    SEARCH_SETTINGS,
    fold_query,
    is_searchable,
    may_match_login,
    search_users,
)

from .helpers import (
    ME,
    PASSWORD,
    SignedInUser,
    execute,
    limited_client,
    verified_user,
)
from .search_helpers import (
    SEARCH,
    Person,
    add_people,
    add_person,
    search,
    set_person_status,
    usernames,
    walk,
)
from .social_helpers import (
    SUMMARY_KEYS,
    befriend,
    block,
    code_of,
    follow,
    send_request,
    set_private,
    unblock,
)


@pytest.fixture
async def viewer(client: httpx.AsyncClient, jobs: InMemoryJobQueue) -> SignedInUser:
    return await verified_user(client, jobs)


# ----------------------------------------------------------------------------- порядок выдачи
async def test_exact_login_comes_first_then_prefixes_then_similar_people(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("anna", "Борис Крылов"),  # точное совпадение ника
            Person("annabel", "Белла"),  # префикс ника, сходство ниже
            Person("anna_k", "Кира"),  # префикс ника, сходство выше
            Person("bob", "Anna Smith"),  # имя совпадает целым словом
            Person("hanna", "Ханна"),  # ник похож, но не начинается с запроса
            Person("dave", "Дэвид"),  # не похож
        ],
    )

    assert usernames(await search(client, viewer, "anna")) == [
        "anna",
        "anna_k",
        "annabel",
        "bob",
        "hanna",
    ]


async def test_the_class_decides_before_the_similarity(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    """Префикс ника с плохим сходством всё равно выше человека, чьё имя совпадает целиком."""
    await add_people(
        admin_engine,
        [
            Person("ivan_long_username_here", "Зоя"),  # префикс `ivan`, сходство низкое
            Person("zed", "Ivan"),  # имя совпадает идеально (мера 2,0), ник нет
            Person("ivan", "Тимур"),  # точный ник
            Person("cyr", "Иван"),  # то же имя кириллицей
        ],
    )

    assert usernames(await search(client, viewer, "ivan")) == [
        "ivan",
        "ivan_long_username_here",
        "zed",
    ]
    # По-русски ветки ника нет вовсе, находится только человек с русским именем.
    assert usernames(await search(client, viewer, "иван")) == ["cyr"]


async def test_inside_a_class_people_follow_by_similarity_then_by_login(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("kate_c", "Икс"),
            Person("kate_b", "Игрек"),
            Person("kate_a_much_longer", "Зет"),
            Person("kate", "Вэ"),
        ],
    )

    # Точный ник первый; префиксы: короче ник значит выше сходство; равные сходства по нику.
    assert usernames(await search(client, viewer, "kate")) == [
        "kate",
        "kate_b",
        "kate_c",
        "kate_a_much_longer",
    ]


async def test_equal_names_are_ordered_by_login_so_pages_are_stable(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person(f"t{n:02d}", "Иван Тестов") for n in (7, 3, 9, 1, 5)])

    first = usernames(await search(client, viewer, "иван тестов"))
    again = usernames(await search(client, viewer, "иван тестов"))

    assert first == again == ["t01", "t03", "t05", "t07", "t09"]


async def test_a_search_matches_the_login_name_or_both(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("ivan_petrov", "Кто-то Другой"),  # слово внутри ника
            Person("zzz", "Petrov Ivan"),  # имя латиницей
            Person("petrov_p", "Petrov Pavel"),  # и ник, и имя: в выдаче один раз, группой выше
            Person("qqq", "Совсем Не То"),
        ],
    )

    found = usernames(await search(client, viewer, "petrov"))

    assert found[0] == "petrov_p"  # ник начинается с запроса: группа 1 выше сходства
    assert sorted(found) == ["ivan_petrov", "petrov_p", "zzz"]  # каждый ровно один раз


# ----------------------------------------------------------------------------- русские имена
async def test_case_is_ignored_in_the_query_and_in_the_names(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("pet_1", "ПЁТР ИВАНОВ"),
            Person("pet_2", "пётр сидоров"),
            Person("pet_3", "Пётр Кузнецов"),
        ],
    )

    for q in ("пётр", "ПЁТР", "Пётр", "пЁтР"):
        assert set(usernames(await search(client, viewer, q))) == {"pet_1", "pet_2", "pet_3"}, q
    assert usernames(await search(client, viewer, "ИВАНОВ"))[0] == "pet_1"


async def test_yo_and_ye_are_the_same_letter_in_both_directions(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [Person("with_yo", "Пётр Ёлкин"), Person("with_ye", "Петр Елкин"), Person("other", "Иван")],
    )

    for q in ("петр елкин", "пётр ёлкин", "Петр Ёлкин", "ПЁТР ЕЛКИН"):
        assert set(usernames(await search(client, viewer, q))) == {"with_yo", "with_ye"}, q
    assert set(usernames(await search(client, viewer, "ёлкин"))) == {"with_yo", "with_ye"}
    assert set(usernames(await search(client, viewer, "елкин"))) == {"with_yo", "with_ye"}


async def test_first_name_and_surname_may_come_in_any_order(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("ord_1", "Петров Иван"),
            Person("ord_2", "Иван Петров"),
            Person("ord_3", "Иван Сергеевич Петров"),  # лишнее слово: ниже
            Person("ord_4", "Мария Петрова"),
            Person("ord_5", "Иван Смирнов"),
        ],
    )

    for q in ("иван петров", "петров иван", "Петров Иван"):
        found = usernames(await search(client, viewer, q))
        assert found[:2] == ["ord_1", "ord_2"], q  # полное совпадение набора слов, равные по нику
        assert found.index("ord_3") == 2  # третье слово понижает
        assert "ord_5" in found  # одно слово совпало: ниже, но в выдаче


async def test_the_start_of_a_word_and_a_typo_find_the_person(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("wrd_1", "Иван Петров"),
            Person("wrd_2", "Марк Иванов"),
            Person("wrd_3", "Анна Сидорова"),
            Person("wrd_4", "Олег Кузнецов"),
        ],
    )

    assert set(usernames(await search(client, viewer, "ив"))) == {"wrd_1", "wrd_2"}  # два символа
    assert set(usernames(await search(client, viewer, "ива"))) == {"wrd_1", "wrd_2"}
    assert "wrd_2" in usernames(await search(client, viewer, "ивонов"))  # опечатка в фамилии
    assert "wrd_4" in usernames(await search(client, viewer, "кузнецоф"))
    assert usernames(await search(client, viewer, "сидорова анна")) == ["wrd_3"]
    assert usernames(await search(client, viewer, "йцукенгшщзхъ")) == []  # нечего найти


async def test_latin_names_and_mixed_scripts(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [
            Person("lat_1", "John Smith"),
            Person("lat_2", "Smith Johnson"),
            Person("lat_3", "Zoë Müller"),
            Person("lat_4", "Иван John"),
        ],
    )

    assert usernames(await search(client, viewer, "john smith"))[0] == "lat_1"
    assert set(usernames(await search(client, viewer, "smi"))) == {"lat_1", "lat_2"}
    assert "lat_3" in usernames(await search(client, viewer, "zoe muller"))  # без диакритики тоже
    assert "lat_4" in usernames(await search(client, viewer, "john"))


async def test_a_decomposed_unicode_query_is_normalized_like_the_names(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_person(admin_engine, "yohan", "Йохан Ёлкин")
    decomposed = unicodedata.normalize("NFD", "йохан")
    assert decomposed != "йохан"

    assert usernames(await search(client, viewer, decomposed)) == ["yohan"]
    assert usernames(await search(client, viewer, "  ЙОХАН  ")) == ["yohan"]  # края обрезаны


async def test_wildcards_in_the_query_are_ordinary_characters(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(
        admin_engine,
        [Person("ab_cd", "Икс"), Person("abxcd", "Игрек"), Person("qqq", "Ab Cd")],
    )

    # `_` в запросе буква ника, а не «любой символ»: `abxcd` не начинается с `ab_cd`. Будь `_`
    # шаблоном, он стал бы префиксом (группа выше) и обогнал бы `qqq`, чьё имя совпадает целиком.
    assert usernames(await search(client, viewer, "ab_cd")) == ["ab_cd", "qqq", "abxcd"]
    for wild in ("%%", "__", "\\\\", "' OR 1=1 --", '"; DROP TABLE x; --', "a%", "%a"):
        response = await search(client, viewer, wild)
        assert response.status_code == 200, (wild, response.text)
    assert usernames(await search(client, viewer, "%%")) == []  # `%` не «все»
    assert usernames(await search(client, viewer, "--")) == []  # из пунктуации ничего не составить


# ----------------------------------------------------------------------------- фильтры
async def test_only_active_accounts_are_found(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    ids = await add_people(
        admin_engine,
        [
            Person("alive", "Тестовый Человек"),
            Person("waiting", "Тестовый Человек", status="pending"),
            Person("suspended", "Тестовый Человек", status="suspended"),
            Person("banned", "Тестовый Человек", status="banned"),
            Person("leaving", "Тестовый Человек", status="deletion_pending"),
        ],
    )

    assert usernames(await search(client, viewer, "тестовый человек")) == ["alive"]
    # Ушедший по нику тоже не находится, хотя ник совпадает точно.
    assert usernames(await search(client, viewer, "banned")) == []
    # Вернулся: находится снова.
    await set_person_status(admin_engine, ids["leaving"], "active")
    assert usernames(await search(client, viewer, "тестовый человек")) == ["alive", "leaving"]


async def test_the_viewer_does_not_find_himself(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_person(admin_engine, "someone_else", "Тестер Один")
    own = viewer.credentials["username"]
    await execute(
        admin_engine,
        "UPDATE profile.profiles SET display_name = 'Тестер Два' WHERE user_id = :id",
        id=uuid.UUID(viewer.user_id),
    )

    assert usernames(await search(client, viewer, own)) == []  # ни по нику
    assert usernames(await search(client, viewer, "тестер")) == ["someone_else"]  # ни по имени


async def test_people_blocked_in_either_direction_are_not_found(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, viewer: SignedInUser
) -> None:
    mine = await verified_user(client, jobs, username="blocked_by_me")
    theirs = await verified_user(client, jobs, username="blocked_me")
    await verified_user(client, jobs, username="blocks_nobody")
    assert (await block(client, viewer, mine)).status_code == 204
    assert (await block(client, theirs, viewer)).status_code == 204

    found = usernames(await search(client, viewer, "block"))

    assert found == ["blocks_nobody"]
    # Для заблокировавшего взгляд обратный: viewer для него тоже исчез.
    assert "blocked_by_me" not in usernames(await search(client, viewer, "blocked_by_me"))
    assert usernames(await search(client, theirs, viewer.credentials["username"])) == []
    assert usernames(await search(client, mine, viewer.credentials["username"])) == []
    # Снятие блокировки возвращает человека в выдачу (дружба при этом не возвращается).
    assert (await unblock(client, viewer, mine)).status_code == 204
    assert set(usernames(await search(client, viewer, "block"))) == {
        "blocked_by_me",
        "blocks_nobody",
    }


async def test_a_block_between_other_people_does_not_hide_anyone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, viewer: SignedInUser
) -> None:
    left = await verified_user(client, jobs, username="side_left")
    right = await verified_user(client, jobs, username="side_right")
    assert (await block(client, left, right)).status_code == 204

    assert set(usernames(await search(client, viewer, "side"))) == {"side_left", "side_right"}


async def test_closed_profiles_are_found_by_the_basic_category_only(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_person(
        admin_engine,
        "closed",
        "Закрытый Человек",
        private=True,
        bio="секретное описание",
        city="Секретоград",
    )

    response = await search(client, viewer, "закрытый")

    assert usernames(response) == ["closed"]
    item = response.json()["items"][0]
    assert set(item) == {"user", "relationship"}
    assert set(item["user"]) == SUMMARY_KEYS  # ник, имя, аватар: категория `basic`
    for secret in ("секретное", "Секретоград", "bio", "city", "birth", "links", "is_private"):
        assert secret not in response.text


# ----------------------------------------------------------------------------- relationship
async def test_each_found_person_carries_the_relationship_to_the_viewer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, viewer: SignedInUser
) -> None:
    friend = await verified_user(client, jobs, username="rel_friend")
    asked = await verified_user(client, jobs, username="rel_asked")
    asking = await verified_user(client, jobs, username="rel_asking")
    await verified_user(client, jobs, username="rel_stranger")
    await befriend(client, viewer, friend)
    sent = await send_request(client, viewer, asked)
    received = await send_request(client, asking, viewer)

    response = await search(client, viewer, "rel_")

    by_login = {item["user"]["username"]: item["relationship"] for item in response.json()["items"]}
    assert set(by_login) == {"rel_friend", "rel_asked", "rel_asking", "rel_stranger"}
    assert by_login["rel_friend"]["friendship"] == "friends"
    assert by_login["rel_friend"]["friend_request_id"] is None
    assert by_login["rel_asked"]["friendship"] == "request_sent"
    assert by_login["rel_asked"]["friend_request_id"] == sent.json()["id"]
    assert by_login["rel_asking"]["friendship"] == "request_received"
    assert by_login["rel_asking"]["friend_request_id"] == received.json()["id"]
    assert by_login["rel_stranger"]["friendship"] == "none"
    for relationship in by_login.values():
        assert set(relationship) == {
            "is_self",
            "friendship",
            "friend_request_id",
            "following",
            "follows_you",
            "blocked",
        }
        assert relationship["is_self"] is False
        assert relationship["blocked"] is False


async def test_found_people_carry_the_follow_state_in_the_relationship(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, viewer: SignedInUser
) -> None:
    followed = await verified_user(client, jobs, username="fol_followed")
    asked = await verified_user(client, jobs, username="fol_asked")
    follower = await verified_user(client, jobs, username="fol_follower")
    await verified_user(client, jobs, username="fol_stranger")
    await set_private(client, asked, True)
    assert (await follow(client, viewer, followed)).json() == {"status": "following"}
    assert (await follow(client, viewer, asked)).json() == {"status": "requested"}
    assert (await follow(client, follower, viewer)).json() == {"status": "following"}

    response = await search(client, viewer, "fol_")

    by_login = {item["user"]["username"]: item["relationship"] for item in response.json()["items"]}
    assert set(by_login) == {"fol_followed", "fol_asked", "fol_follower", "fol_stranger"}
    assert (by_login["fol_followed"]["following"], by_login["fol_followed"]["follows_you"]) == (
        "following",
        False,
    )
    assert by_login["fol_asked"]["following"] == "requested"
    assert (by_login["fol_follower"]["following"], by_login["fol_follower"]["follows_you"]) == (
        "none",
        True,
    )
    assert (by_login["fol_stranger"]["following"], by_login["fol_stranger"]["follows_you"]) == (
        "none",
        False,
    )


# ----------------------------------------------------------------------------- страницы
async def test_pages_follow_one_another_without_losses_or_repeats(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person(f"p{n:03d}", "Иван Тестов") for n in range(45)])
    expected = [f"p{n:03d}" for n in range(45)]

    first = (await search(client, viewer, "иван тестов", limit=20)).json()
    second = (await search(client, viewer, "иван тестов", limit=20, offset=20)).json()
    third = (await search(client, viewer, "иван тестов", limit=20, offset=40)).json()

    assert first["next_offset"] == 20
    assert second["next_offset"] == 40
    assert third["next_offset"] is None  # на последней странице пять человек
    walked = [item["user"]["username"] for page in (first, second, third) for item in page["items"]]
    assert walked == expected
    assert len(third["items"]) == 5
    assert await walk(client, viewer, "иван тестов", limit=7) == expected


async def test_a_page_exactly_at_the_end_has_no_next_offset(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person(f"ex{n:02d}", "Ровно Двадцать") for n in range(20)])

    page = (await search(client, viewer, "ровно двадцать", limit=20)).json()

    assert len(page["items"]) == 20
    assert page["next_offset"] is None  # двадцать из двадцати: следующей страницы нет
    short = (await search(client, viewer, "ровно двадцать", limit=19)).json()
    assert short["next_offset"] == 19  # двадцатый ещё впереди


async def test_the_depth_is_capped_at_200_results(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person(f"d{n:03d}", "Глубокий Поиск") for n in range(205)])

    last_allowed = (await search(client, viewer, "глубокий поиск", limit=50, offset=150)).json()
    assert len(last_allowed["items"]) == 50
    assert last_allowed["next_offset"] is None  # потолок достигнут, хотя люди ещё есть
    assert last_allowed["items"][-1]["user"]["username"] == "d199"
    assert len(await walk(client, viewer, "глубокий поиск")) == 200  # дальше 200 не листается

    over = await search(client, viewer, "глубокий поиск", limit=50, offset=151)
    assert code_of(over) == (422, "validation_error")
    item = over.json()["errors"][0]
    assert (item["pointer"], item["code"]) == ("/query/limit", "out_of_range")
    assert item["meta"] == {"max": 49}  # сколько ещё можно попросить при таком смещении
    beyond = await search(client, viewer, "глубокий поиск", limit=1, offset=200)
    assert code_of(beyond) == (422, "validation_error")
    assert beyond.json()["errors"][0]["pointer"] == "/query/offset"
    assert beyond.json()["errors"][0]["meta"] == {"max": 199}
    edge = await search(client, viewer, "глубокий поиск", limit=1, offset=199)
    assert edge.status_code == 200
    assert edge.json()["next_offset"] is None


async def test_the_next_offset_is_offered_until_the_ceiling_even_when_limit_no_longer_fits(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    """Контракт: `next_offset` это `offset + limit`; а `limit` следующего запроса клиент уменьшает сам."""
    await add_people(admin_engine, [Person(f"s{n:03d}", "Шаг Семь") for n in range(210)])

    page = (await search(client, viewer, "шаг семь", limit=7, offset=189)).json()

    assert page["next_offset"] == 196
    same_limit = await search(client, viewer, "шаг семь", limit=7, offset=196)
    assert code_of(same_limit) == (422, "validation_error")
    fitting = await search(client, viewer, "шаг семь", limit=4, offset=196)
    assert fitting.status_code == 200
    assert fitting.json()["next_offset"] is None


async def test_cutting_each_source_never_drops_a_better_group(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    """Подзапрос по нику оставляет первые `offset + limit` строк (здесь три) в том же порядке, что и итог:
    группа раньше меры. Если бы он резал по мере, префикс с низкой мерой вылетел бы раньше трёх сходных
    ников с высокой, и вторая строка первой страницы оказалась бы не той."""
    await add_people(
        admin_engine,
        [
            Person("ivan", "Один"),  # точный ник: группа 0
            Person("ivan_long_username_here", "Два"),  # префикс, мера низкая: группа 1
            Person("xivan", "Три"),  # группа 2 с мерой 0,875
            Person("zivan", "Четыре"),
            Person("qivan", "Пять"),
        ],
    )

    first = await search(client, viewer, "ivan", limit=2)
    second = await search(client, viewer, "ivan", limit=2, offset=2)

    assert usernames(first) == ["ivan", "ivan_long_username_here"]
    assert usernames(second) == ["qivan", "xivan"]
    assert await walk(client, viewer, "ivan", limit=2) == [
        "ivan",
        "ivan_long_username_here",
        "qivan",
        "xivan",
        "zivan",
    ]


# ----------------------------------------------------------------------------- проверка входа
async def test_the_query_must_have_two_to_fifty_characters_after_normalization(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_person(admin_engine, "ok_person", "Нормальный Человек")

    for short in ("а", " а ", "   ", ""):
        refused = await search(client, viewer, short)
        assert code_of(refused) == (422, "validation_error"), repr(short)
        item = refused.json()["errors"][0]
        assert (item["pointer"], item["code"]) == ("/query/q", "string_too_short"), repr(short)
        assert item["meta"] == {"min_length": 2}
    long = await search(client, viewer, "я" * 51)
    assert code_of(long) == (422, "validation_error")
    item = long.json()["errors"][0]
    assert (item["pointer"], item["code"], item["meta"]) == (
        "/query/q",
        "string_too_long",
        {"max_length": 50},
    )
    assert (await search(client, viewer, "я" * 50)).status_code == 200
    assert (await search(client, viewer, "аб")).status_code == 200
    # Длина считается после обрезки краёв: пробелы её не увеличивают.
    assert (await search(client, viewer, "  " + "я" * 50 + "  ")).status_code == 200
    assert code_of(await search(client, viewer, "  " + "я" * 51 + "  "))[0] == 422
    # Без параметра вовсе.
    missing = await client.get(SEARCH, headers=viewer.headers)
    assert code_of(missing) == (422, "validation_error")
    assert missing.json()["errors"][0]["code"] == "required"


@pytest.mark.parametrize("bad", ["\x00", "ab\x00cd", "\x07ab", "ab\x1b", "a\x7fb"])
async def test_control_characters_and_nul_are_refused(
    client: httpx.AsyncClient, viewer: SignedInUser, bad: str
) -> None:
    refused = await search(client, viewer, bad)

    assert code_of(refused) == (422, "validation_error"), repr(bad)
    assert refused.json()["errors"][0]["code"] == "invalid_format"


@pytest.mark.parametrize(
    ("params", "pointer", "code"),
    [
        ({"limit": 0}, "/query/limit", "out_of_range"),
        ({"limit": 51}, "/query/limit", "out_of_range"),
        ({"limit": -3}, "/query/limit", "out_of_range"),
        ({"limit": "много"}, "/query/limit", "invalid_format"),
        ({"limit": "2.5"}, "/query/limit", "invalid_format"),
        ({"offset": -1}, "/query/offset", "out_of_range"),
        ({"offset": "abc"}, "/query/offset", "invalid_format"),
        ({"offset": "1e3"}, "/query/offset", "invalid_format"),
        ({"offset": 10**30}, "/query/offset", "out_of_range"),
        ({"offset": 10**30, "limit": 50}, "/query/offset", "out_of_range"),
    ],
)
async def test_limit_and_offset_are_validated(
    client: httpx.AsyncClient,
    viewer: SignedInUser,
    params: dict[str, object],
    pointer: str,
    code: str,
) -> None:
    refused = await search(client, viewer, "тест", **params)

    assert code_of(refused) == (422, "validation_error"), params
    pointers = {(item["pointer"], item["code"]) for item in refused.json()["errors"]}
    assert (pointer, code) in pointers, (params, pointers)


async def test_defaults_are_twenty_per_page_from_the_start(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person(f"f{n:02d}", "Двадцать Один") for n in range(25)])

    page = (await search(client, viewer, "двадцать один")).json()

    assert len(page["items"]) == 20
    assert page["next_offset"] == 20


async def test_an_empty_result_is_a_normal_page(
    client: httpx.AsyncClient, viewer: SignedInUser
) -> None:
    response = await search(client, viewer, "никого нет")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_offset": None}


# ----------------------------------------------------------------------------- доступ и заголовки
async def test_a_token_is_required(client: httpx.AsyncClient) -> None:
    anonymous = await client.get(SEARCH, params={"q": "анна"})
    assert code_of(anonymous) == (401, "token_missing")
    forged = await client.get(
        SEARCH, params={"q": "анна"}, headers={"Authorization": "Bearer not.a.token"}
    )
    assert code_of(forged) == (401, "token_invalid")


async def test_an_account_waiting_for_deletion_cannot_search(
    client: httpx.AsyncClient, viewer: SignedInUser
) -> None:
    deleted = await client.request(
        "DELETE", ME, json={"password": PASSWORD}, headers=viewer.headers
    )
    assert deleted.status_code == 202, deleted.text

    assert code_of(await search(client, viewer, "анна")) == (403, "account_deletion_pending")


async def test_responses_are_never_cached_and_errors_too(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    await add_person(admin_engine, "cached", "Кэшированный")

    ok = await search(client, viewer, "кэшированный")
    bad = await search(client, viewer, "я")

    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"
    assert bad.status_code == 422
    assert bad.headers["cache-control"] == "no-store"


async def test_the_endpoint_is_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    operation = schema["paths"]["/api/v1/search/users"]["get"]
    assert {"200", "401", "403", "422", "429", "503"} <= set(operation["responses"])
    assert "Найти людей" in operation["summary"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert set(parameters) == {"q", "limit", "offset"}
    assert parameters["q"]["required"] is True
    assert (parameters["q"]["schema"]["minLength"], parameters["q"]["schema"]["maxLength"]) == (
        2,
        50,
    )
    assert (parameters["limit"]["schema"]["default"], parameters["limit"]["schema"]["maximum"]) == (
        20,
        50,
    )
    assert parameters["offset"]["schema"]["default"] == 0
    page = schema["components"]["schemas"]["UserSearchPage"]
    assert page["examples"][0]["next_offset"] == 20
    assert set(page["required"]) == {"items", "next_offset"}
    assert "UserSearchItem" in schema["components"]["schemas"]


# ----------------------------------------------------------------------------- лимиты
async def test_search_has_its_own_limit_and_reports_it(
    test_settings: Settings, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    async with limited_client(test_settings, jobs, search=3) as (_, http):
        viewer = await verified_user(http, jobs)
        await add_person(admin_engine, "limited", "Лимитированный")
        answers = [await search(http, viewer, "лимитированный") for _ in range(4)]
        other = await verified_user(http, jobs)
        neighbour = await search(http, other, "лимитированный")
        read_only = await http.get(ME, headers=viewer.headers)  # другой бакет (`api_read`) жив

    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert [a.headers["ratelimit-limit"] for a in answers[:3]] == ["3", "3", "3"]
    assert [a.headers["ratelimit-remaining"] for a in answers[:3]] == ["2", "1", "0"]
    assert answers[0].headers["ratelimit-reset"]
    limited = answers[3]
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0
    assert limited.headers["cache-control"] == "no-store"
    assert neighbour.status_code == 200  # лимит по человеку: чужой запрос не задет
    assert read_only.status_code == 200  # лимит поиска не трогает остальные чтения


async def test_search_also_spends_the_general_read_limit(
    test_settings: Settings, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    async with limited_client(test_settings, jobs, search=100, api_read=2) as (_, http):
        viewer = await verified_user(http, jobs)
        await add_person(admin_engine, "limited2", "Второй Лимит")
        answers = [await search(http, viewer, "второй лимит") for _ in range(3)]

    assert [a.status_code for a in answers] == [200, 200, 429]
    assert answers[0].headers["ratelimit-limit"] == "2"  # показан более тесный бакет


# ----------------------------------------------------------------------------- модель-эталон
SYLLABLES = (
    "an",
    "ni",
    "iv",
    "va",
    "pe",
    "tr",
    "ro",
    "ol",
    "le",
    "eg",
    "ko",
    "zu",
    "ma",
    "ri",
    "bo",
)
STATUSES = ("active",) * 8 + ("suspended", "banned", "deletion_pending", "pending")


def random_login(rng: random.Random, taken: set[str]) -> str:
    while True:
        login = "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3)))
        login += rng.choice(["", "", "_" + rng.choice(SYLLABLES), str(rng.randint(0, 99))])
        if login not in taken and 3 <= len(login) <= 30:
            taken.add(login)
            return login


def random_queries(rng: random.Random, people: list[Person], count: int) -> list[str]:
    """Запросы разного вида: начало ника, ник, слово имени, его начало, опечатка, слова наоборот."""
    queries: list[str] = []
    while len(queries) < count:
        person = rng.choice(people)
        words = [w for w in re.split(r"\W+", fold_query(person.name)) if w]
        word = rng.choice(words) if words else person.name
        kind = rng.randrange(7)
        if kind == 0:
            query = person.username[: rng.randint(2, 4)]
        elif kind == 1:
            query = person.username
        elif kind == 2:
            query = word
        elif kind == 3:
            query = word[: rng.randint(2, 4)]
        elif kind == 4 and len(word) >= 4:
            at = rng.randrange(1, len(word) - 2)
            query = (
                word[:at] + word[at + 1] + word[at] + word[at + 2 :]
            )  # две соседние буквы местами
        elif kind == 5 and len(words) >= 2:
            query = " ".join(reversed(words))
        else:
            query = "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 2)))
        if len(query.strip()) >= 2:
            queries.append(query)
    return queries


async def reference_order(
    engine: AsyncEngine, viewer: uuid.UUID, query: str, hidden: set[uuid.UUID]
) -> list[str]:
    """Порядок выдачи, собранный отдельно от запроса приложения: числа сходства берёт у самой PostgreSQL
    (пересчитывать триграммы в Python значило бы проверять копию, а не запрос), а фильтры, группы,
    сочетание полей и порядок решает простой Python по тексту 4.10."""
    folded = fold_query(query)
    folded_name = "translate(p.display_name, 'ёЁ', 'еЕ')"
    async with engine.begin() as connection:
        for name, value in SEARCH_SETTINGS.items():  # те же пороги, что у приложения
            await connection.execute(
                text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
            )
        rows = (
            await connection.execute(
                text(
                    "SELECT u.id, u.username::text AS login, u.status, "
                    "(u.username::text % :q) AS login_sim_op, (:q <% u.username::text) AS login_word_op, "
                    f"({folded_name} % :q) AS name_sim_op, (:q <% {folded_name}) AS name_word_op, "
                    "similarity(u.username::text, :q) AS login_sim, "
                    "similarity(u.username::text, :q) + word_similarity(:q, u.username::text) AS login_sum, "
                    f"similarity({folded_name}, :q) + word_similarity(:q, {folded_name}) AS name_sum "
                    "FROM identity.users u JOIN profile.profiles p ON p.user_id = u.id"
                ),
                {"q": folded},
            )
        ).all()
    ranked: list[tuple[int, float, str, uuid.UUID]] = []
    for row in rows:
        if row.id == viewer or row.status != "active" or row.id in hidden:
            continue
        group = 0 if row.login == folded else 1 if row.login.startswith(folded) else 2
        by_login = may_match_login(folded) and (group < 2 or row.login_sim_op or row.login_word_op)
        by_name = row.name_sim_op or row.name_word_op
        if not (by_login or by_name):
            continue
        if group < 2:
            score = row.login_sim
        else:
            score = max(
                row.login_sum if by_login else float("-inf"),
                row.name_sum if by_name else float("-inf"),
            )
        ranked.append((group, -score, row.login, row.id))
    ranked.sort()
    return [login for _, _, login, _ in ranked][:200]


async def test_search_agrees_with_an_independent_reference_on_random_people(
    client: httpx.AsyncClient, viewer: SignedInUser, admin_engine: AsyncEngine
) -> None:
    """150 случайных людей (разные статусы, блокировки в обе стороны) и 70 разных запросов: страницы поиска
    слагаются в тот же порядок, что даёт эталон (фильтры, группы, лучшая мера из двух полей, страницы)."""
    rng = random.Random(2026)
    taken: set[str] = {viewer.credentials["username"]}
    people = [
        Person(
            random_login(rng, taken),
            big_display_name(rng)[0],
            status=rng.choice(STATUSES),
            private=rng.random() < 0.2,
        )
        for _ in range(150)
    ]
    ids = await add_people(admin_engine, people)
    viewer_id = uuid.UUID(viewer.user_id)
    active = [person for person in people if person.status == "active"]
    blocked_by_viewer, blocked_viewer = active[:3], active[3:6]
    hidden = {ids[person.username] for person in blocked_by_viewer + blocked_viewer}
    for person in blocked_by_viewer:
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            a=viewer_id,
            b=ids[person.username],
        )
    for person in blocked_viewer:
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            a=ids[person.username],
            b=viewer_id,
        )

    queries = random_queries(rng, people, 70)
    non_empty = 0
    for query in queries:
        expected = await reference_order(admin_engine, viewer_id, query, hidden)
        got = await walk(client, viewer, query, limit=rng.choice([10, 25, 50]))
        assert got == expected, query
        non_empty += bool(expected)
    assert non_empty >= 50  # запросы не пустые: сравнение что-то сравнивает


async def test_a_query_without_letters_does_not_touch_the_database(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("запрос из одной пунктуации дошёл до базы")

    monkeypatch.setattr("messunjerr.social.queries.search.search_statement", refuse)

    async with sessionmaker() as session:
        page = await search_users(session, viewer_id=uuid.uuid4(), q="--", limit=20, offset=0)
        with pytest.raises(AssertionError, match="дошёл до базы"):
            await search_users(session, viewer_id=uuid.uuid4(), q="ab", limit=20, offset=0)

    assert page.items == []
    assert page.next_offset is None


async def test_parallel_searches_neither_disturb_each_other_nor_a_concurrent_block(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    viewer: SignedInUser,
    admin_engine: AsyncEngine,
) -> None:
    await add_people(admin_engine, [Person(f"par{n:02d}", "Параллельный Поиск") for n in range(30)])
    victim = await verified_user(client, jobs, username="par_victim")

    async def blocking() -> int:
        return (await block(client, viewer, victim)).status_code

    results = await asyncio.gather(
        *(search(client, viewer, "параллельный поиск", limit=50) for _ in range(12)),
        *(search(client, viewer, "par", limit=50) for _ in range(12)),
        blocking(),
    )

    responses = [r for r in results if isinstance(r, httpx.Response)]
    assert all(r.status_code == 200 for r in responses)
    names = {tuple(usernames(r)) for r in responses[:12]}
    assert len(names) == 1  # одни и те же тридцать человек в одном порядке
    assert results[-1] == 204
    assert "par_victim" not in usernames(await search(client, viewer, "par", limit=50))


def test_queries_without_letters_cannot_match_and_russian_ones_skip_the_login() -> None:
    assert not is_searchable(fold_query("--"))
    assert not is_searchable(fold_query("%%"))
    assert is_searchable("__")
    assert is_searchable("ё")
    assert may_match_login("ab")
    assert may_match_login("иван_1")
    assert not may_match_login("иван петров")
    assert fold_query("ПЁТР Ёлкин") == "петр елкин"

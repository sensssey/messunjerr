"""S8-07: `make seed-big`: большой набор людей со связями, повтор безопасен, в prod и stage запрещён."""

import asyncio
import uuid
from collections.abc import Callable, Collection
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr import seeding
from messunjerr.cli import build_parser, main
from messunjerr.seeding import (
    BIG_DEFAULT_SEED,
    BIG_DEFAULT_USERS,
    BIG_MAX_USERS,
    SEED_PASSWORD,
    BigGraph,
    BigPopulation,
    BigSeedResult,
    Edge,
    big_email,
    big_username,
    build_big_graph,
    seed_big_database,
)
from messunjerr.settings import Settings

from .helpers import bearer, execute, fetch_all, fetch_one
from .search_helpers import SEARCH, Person, add_people
from .social_helpers import assert_graph_is_consistent

USERS = 200
TABLES = (
    "identity.users",
    "profile.profiles",
    "profile.privacy_settings",
    "social.friendships",
    "social.follows",
    "social.blocks",
    "social.friend_requests",
    "social.follow_requests",
)


async def counts(engine: AsyncEngine) -> dict[str, int]:
    found: dict[str, int] = {}
    for table in TABLES:
        found[table] = (await fetch_one(engine, f"SELECT count(*) AS n FROM {table}"))["n"]
    return found


async def assert_invariants(engine: AsyncEngine) -> None:
    """Инварианты S7–S8 запросами к БД: то, что обязан хранить любой набор данных графа."""
    await assert_graph_is_consistent(engine)  # блокировка/дружба/ждущая заявка (S7)
    problems = {
        "friendship not ordered or with itself": "SELECT 1 FROM social.friendships "
        "WHERE user_low_id >= user_high_id",
        "block together with a follow in either direction": "SELECT 1 FROM social.blocks b "
        "JOIN social.follows f ON (f.follower_id = b.blocker_id AND f.followee_id = b.blocked_id) "
        "OR (f.follower_id = b.blocked_id AND f.followee_id = b.blocker_id)",
        "block together with a pending follow request": "SELECT 1 FROM social.blocks b "
        "JOIN social.follow_requests q ON q.status = 'pending' "
        "AND ((q.follower_id = b.blocker_id AND q.followee_id = b.blocked_id) "
        "OR (q.follower_id = b.blocked_id AND q.followee_id = b.blocker_id))",
        "two blocks for one pair": "SELECT 1 FROM social.blocks "
        "GROUP BY LEAST(blocker_id, blocked_id), GREATEST(blocker_id, blocked_id) HAVING count(*) > 1",
        "two pending friend requests for one pair": "SELECT 1 FROM social.friend_requests "
        "WHERE status = 'pending' GROUP BY LEAST(sender_id, receiver_id), "
        "GREATEST(sender_id, receiver_id) HAVING count(*) > 1",
        "two pending follow requests for one pair": "SELECT 1 FROM social.follow_requests "
        "WHERE status = 'pending' GROUP BY follower_id, followee_id HAVING count(*) > 1",
        "follow request to an open profile": "SELECT 1 FROM social.follow_requests q "
        "JOIN profile.profiles p ON p.user_id = q.followee_id WHERE NOT p.is_private",
        "follow request from someone who already follows": "SELECT 1 FROM social.follow_requests q "
        "JOIN social.follows f ON f.follower_id = q.follower_id AND f.followee_id = q.followee_id "
        "WHERE q.status = 'pending'",
        "pending friend request between friends": "SELECT 1 FROM social.friend_requests r "
        "JOIN social.friendships f ON f.user_low_id = LEAST(r.sender_id, r.receiver_id) "
        "AND f.user_high_id = GREATEST(r.sender_id, r.receiver_id) WHERE r.status = 'pending'",
    }
    for name, sql in problems.items():
        assert await fetch_all(engine, sql + " LIMIT 1") == [], name


def pairs_by_username(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(row["first"], row["second"]) for row in rows}


# ----------------------------------------------------------------------------- состав набора
async def test_everyone_is_active_verified_and_has_a_profile_and_privacy_settings(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    result = await seed_big_database(test_settings, users=USERS)

    assert (result.created, result.existing) == (USERS, 0)
    people = await fetch_all(
        admin_engine,
        "SELECT u.username::text AS username, u.email::text AS email, u.status, "
        "u.email_verified_at IS NOT NULL AS verified, u.terms_version, u.password_hash, "
        "p.display_name FROM identity.users u JOIN profile.profiles p ON p.user_id = u.id "
        "JOIN profile.privacy_settings s ON s.user_id = u.id ORDER BY u.username",
    )
    assert [p["username"] for p in people] == [big_username(n) for n in range(1, USERS + 1)]
    assert people[0]["username"] == "big_00001"
    assert [p["email"] for p in people] == [big_email(n) for n in range(1, USERS + 1)]
    assert all(p["status"] == "active" and p["verified"] for p in people)
    assert {p["terms_version"] for p in people} == {test_settings.legal_terms_version}
    assert len({p["password_hash"] for p in people}) == 1  # пароль общий, хэш один на всех
    assert all(1 <= len(p["display_name"]) <= 50 for p in people)
    assert (
        len({p["display_name"] for p in people}) > USERS * 0.7
    )  # имена разные, повторы естественны


async def test_a_noticeable_share_of_profiles_is_closed_and_the_lists_are_set_all_over_the_place(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=400)

    private = (
        await fetch_one(admin_engine, "SELECT count(*) AS n FROM profile.profiles WHERE is_private")
    )["n"]
    assert 0.14 < private / 400 < 0.26  # около 20%
    visibilities = await fetch_all(
        admin_engine,
        "SELECT friends_list_visibility AS friends, followers_list_visibility AS followers, "
        "count(*) AS n FROM profile.privacy_settings GROUP BY 1, 2",
    )
    assert {row["friends"] for row in visibilities} == {"everyone", "friends", "only_me"}
    assert {row["followers"] for row in visibilities} == {"everyone", "friends", "only_me"}
    assert {
        row["birth_date_visibility"]
        for row in await fetch_all(
            admin_engine, "SELECT birth_date_visibility FROM profile.profiles"
        )
    } == {"hidden", "day_month", "full"}


async def test_the_graph_has_friends_follows_blocks_and_waiting_requests(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    result = await seed_big_database(test_settings, users=USERS)

    stored = await counts(admin_engine)
    graph = build_big_graph(
        USERS, BIG_DEFAULT_SEED, set()
    )  # размеры дружб и подписок от закрытых не зависят
    assert stored["social.friendships"] == len(graph.friendships) == result.friendships == 1500
    assert stored["social.follows"] == len(graph.follows) == result.follows == 6000
    assert stored["social.blocks"] == result.blocks == 4
    assert stored["social.friend_requests"] == result.friend_requests == 80
    assert stored["social.follow_requests"] == result.follow_requests > 0
    waiting = await fetch_all(admin_engine, "SELECT DISTINCT status FROM social.friend_requests")
    assert [row["status"] for row in waiting] == ["pending"]
    degree = await fetch_one(
        admin_engine,
        "SELECT avg(n) AS mean FROM (SELECT count(*) AS n FROM (SELECT user_low_id AS u FROM "
        "social.friendships UNION ALL SELECT user_high_id FROM social.friendships) x GROUP BY u) y",
    )
    assert 12 <= float(degree["mean"]) <= 18  # в среднем около 15 друзей у тех, кто с кем-то дружит


async def test_the_database_invariants_hold_after_seeding(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=USERS)

    await assert_invariants(admin_engine)


async def test_follows_are_skewed_towards_popular_accounts(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=400)

    rows = await fetch_all(
        admin_engine,
        "SELECT count(*) AS followers FROM social.follows GROUP BY followee_id ORDER BY 1 DESC",
    )
    top = rows[0]["followers"]
    median = sorted(row["followers"] for row in rows)[len(rows) // 2]
    assert top >= 8 * median  # у «звёзд» в разы больше подписчиков, чем у обычного человека


async def test_relations_are_dated_between_registration_and_now(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=USERS)

    early = await fetch_all(
        admin_engine,
        "SELECT 1 FROM social.friendships f JOIN identity.users a ON a.id = f.user_low_id "
        "JOIN identity.users b ON b.id = f.user_high_id "
        "WHERE f.created_at < GREATEST(a.created_at, b.created_at) OR f.created_at > now() LIMIT 1",
    )
    assert early == []  # дружба не старше аккаунтов и не из будущего
    spread = await fetch_one(
        admin_engine,
        "SELECT count(DISTINCT date_trunc('day', created_at)) AS days FROM social.friendships",
    )
    assert spread["days"] > 100  # даты разбросаны, а не равны моменту запуска


# ----------------------------------------------------------------------------- повтор и рост
async def test_the_same_run_again_changes_nothing(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    first = await seed_big_database(test_settings, users=USERS)
    before = await counts(admin_engine)
    ids_before = await fetch_all(admin_engine, "SELECT id FROM identity.users ORDER BY id")

    again = await seed_big_database(test_settings, users=USERS)

    assert (again.created, again.existing) == (0, USERS)
    assert (again.friendships, again.follows, again.blocks) == (0, 0, 0)
    assert (again.friend_requests, again.follow_requests) == (0, 0)
    assert await counts(admin_engine) == before
    assert await fetch_all(admin_engine, "SELECT id FROM identity.users ORDER BY id") == ids_before
    assert first.created == USERS
    await assert_invariants(admin_engine)


async def test_a_larger_run_adds_people_and_keeps_every_invariant(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=80)
    first_people = await fetch_all(
        admin_engine, "SELECT id, username::text AS u FROM identity.users"
    )

    more = await seed_big_database(test_settings, users=USERS)

    assert (more.created, more.existing) == (USERS - 80, 80)
    assert (await counts(admin_engine))["identity.users"] == USERS
    kept = await fetch_all(admin_engine, "SELECT id, username::text AS u FROM identity.users")
    assert {(row["id"], row["u"]) for row in first_people} <= {
        (row["id"], row["u"]) for row in kept
    }
    await assert_invariants(admin_engine)


async def test_a_run_with_another_seed_adds_links_without_breaking_invariants(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=USERS, seed=1)
    before = await counts(admin_engine)

    other = await seed_big_database(test_settings, users=USERS, seed=2)

    after = await counts(admin_engine)
    assert other.created == 0
    assert after["identity.users"] == before["identity.users"]  # люди те же
    assert after["social.friendships"] > before["social.friendships"]  # связей стало больше
    await assert_invariants(admin_engine)  # блокировки нового зерна не стали дружбой прежнего


async def test_the_data_depends_on_the_seed_only(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    friends_sql = (
        "SELECT a.username::text AS first, b.username::text AS second FROM social.friendships f "
        "JOIN identity.users a ON a.id = f.user_low_id JOIN identity.users b ON b.id = f.user_high_id"
    )
    names_sql = (
        "SELECT u.username::text AS first, p.display_name AS second FROM identity.users u "
        "JOIN profile.profiles p ON p.user_id = u.id"
    )
    await seed_big_database(test_settings, users=120)
    friends = pairs_by_username(await fetch_all(admin_engine, friends_sql))
    friends = {tuple(sorted(pair)) for pair in friends}
    names = pairs_by_username(await fetch_all(admin_engine, names_sql))
    await execute(admin_engine, "TRUNCATE identity.users CASCADE")

    await seed_big_database(test_settings, users=120)  # новые UUID, те же данные

    again = {
        tuple(sorted(pair))
        for pair in pairs_by_username(await fetch_all(admin_engine, friends_sql))
    }
    assert again == friends
    assert pairs_by_username(await fetch_all(admin_engine, names_sql)) == names


async def test_pairs_are_stored_in_uuid_order_whatever_the_order_the_people_were_created_in(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    """Номера людей и их UUID не обязаны идти в одном порядке (UUIDv7 растут вместе с номерами, но аккаунт могли
    пересоздать или завести иначе): дружба хранится «меньший UUID первым» по самим UUID. Люди здесь заведены
    заранее с UUIDv4, то есть в случайном порядке."""
    await add_people(admin_engine, [Person(big_username(n), f"Человек {n}") for n in range(1, 41)])

    result = await seed_big_database(test_settings, users=40)

    assert result.created == 0  # ники заняты: люди прежние
    assert result.friendships > 0
    wrong = await fetch_all(
        admin_engine, "SELECT 1 FROM social.friendships WHERE user_low_id >= user_high_id LIMIT 1"
    )
    assert wrong == []
    against_numbers = await fetch_one(
        admin_engine,
        "SELECT count(*) AS n FROM social.friendships f "
        "JOIN identity.users a ON a.id = f.user_low_id JOIN identity.users b ON b.id = f.user_high_id "
        "WHERE a.username::text > b.username::text",
    )
    assert against_numbers["n"] > 0  # порядок UUID и вправду не совпал с порядком номеров
    await assert_invariants(admin_engine)


def graph_returning(graph: BigGraph) -> Callable[[int, int, Collection[int]], BigGraph]:
    """Подмена генератора: тест подсовывает вставке заранее заданный граф."""

    def build(users: int, seed: int, private: Collection[int]) -> BigGraph:
        return graph

    return build


async def by_number(engine: AsyncEngine) -> dict[int, uuid.UUID]:
    rows = await fetch_all(engine, "SELECT id, username::text AS u FROM identity.users")
    return {int(row["u"][4:]): row["id"] for row in rows}


async def test_every_insert_refuses_a_link_that_breaks_the_invariants_on_its_own(
    test_settings: Settings, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прежний запуск мог оставить другие связи, поэтому каждая вставка сама проверяет пару. Здесь база
    подготовлена вручную, а генератор подменён графом, где каждая связь запрещена чем-то, что уже лежит
    в таблицах (кроме пяти контрольных, которые обязаны пройти)."""
    monkeypatch.setattr(seeding, "build_big_graph", graph_returning(BigGraph()))
    await seed_big_database(test_settings, users=20)  # люди без единой связи
    ids = await by_number(admin_engine)
    await execute(admin_engine, "UPDATE profile.profiles SET is_private = false")
    for number in range(1, 11):  # закрыты первые десять
        await execute(
            admin_engine,
            "UPDATE profile.profiles SET is_private = true WHERE user_id = :id",
            id=ids[number],
        )
    low, high = sorted((ids[1], ids[2]), key=lambda value: value.int)
    await execute(
        admin_engine,
        "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
        a=low,
        b=high,
    )
    await execute(
        admin_engine,
        "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
        a=ids[3],
        b=ids[4],
    )
    await execute(
        admin_engine,
        "INSERT INTO social.follows (follower_id, followee_id) VALUES (:a, :b)",
        a=ids[5],
        b=ids[6],
    )
    await execute(
        admin_engine,
        "INSERT INTO social.friend_requests (sender_id, receiver_id) VALUES (:a, :b)",
        a=ids[7],
        b=ids[8],
    )
    await execute(
        admin_engine,
        "INSERT INTO social.follow_requests (follower_id, followee_id) VALUES (:a, :b)",
        a=ids[9],
        b=ids[10],
    )
    before = await counts(admin_engine)

    def e(first: int, second: int) -> Edge:
        return Edge(first, second, 0.5)

    hostile = BigGraph(
        blocks=(
            e(1, 2),  # они друзья
            e(5, 6),  # один подписан на другого
            e(7, 8),  # ждёт заявка в друзья
            e(9, 10),  # ждёт запрос на подписку
            e(4, 3),  # блокировка уже есть, встречной не бывает
            e(11, 12),  # контрольная: пара чистая
        ),
        friendships=(
            e(3, 4),  # заблокированы
            e(7, 8),  # ждёт заявка
            e(13, 14),  # контрольная
        ),
        follows=(
            e(3, 4),  # заблокированы
            e(4, 3),  # заблокированы в обратную сторону
            e(9, 10),  # ждёт запрос на подписку
            e(15, 16),  # контрольная
        ),
        friend_requests=(
            e(1, 2),  # уже друзья
            e(3, 4),  # заблокированы
            e(8, 7),  # встречная ждущая заявка пары уже есть
            e(17, 18),  # контрольная
        ),
        follow_requests=(
            e(3, 4),  # закрытый профиль, но пара заблокирована
            e(5, 6),  # закрытый профиль, но уже подписан
            e(19, 20),  # профиль открытый: запрос не нужен (пара чистая, отказывает только это)
            e(13, 2),  # контрольный: закрытый профиль, пара чистая
        ),
    )
    monkeypatch.setattr(seeding, "build_big_graph", graph_returning(hostile))

    result = await seed_big_database(test_settings, users=20)

    assert (result.created, result.blocks, result.friendships, result.follows) == (0, 1, 1, 1)
    assert (result.friend_requests, result.follow_requests) == (1, 1)
    after = await counts(admin_engine)
    for table in ("social.blocks", "social.friendships", "social.follows"):
        assert after[table] == before[table] + 1, table
    assert after["social.friend_requests"] == before["social.friend_requests"] + 1
    assert after["social.follow_requests"] == before["social.follow_requests"] + 1
    assert after["identity.users"] == before["identity.users"]
    added = await fetch_all(
        admin_engine,
        "SELECT 'block' AS kind, a.username::text AS first, b.username::text AS second "
        "FROM social.blocks k JOIN identity.users a ON a.id = k.blocker_id "
        "JOIN identity.users b ON b.id = k.blocked_id WHERE a.username::text IN "
        "('big_00011', 'big_00004') "
        "UNION ALL SELECT 'follow_request', a.username::text, b.username::text "
        "FROM social.follow_requests q JOIN identity.users a ON a.id = q.follower_id "
        "JOIN identity.users b ON b.id = q.followee_id WHERE a.username::text = 'big_00013'",
    )
    assert {(row["kind"], row["first"], row["second"]) for row in added} == {
        ("block", "big_00011", "big_00012"),
        ("follow_request", "big_00013", "big_00002"),
    }
    await assert_invariants(admin_engine)


# ----------------------------------------------------------------------------- запреты и аргументы
@pytest.mark.parametrize("env", ["prod", "stage"])
async def test_seeding_is_refused_outside_development(test_settings: Settings, env: str) -> None:
    settings = test_settings.model_copy(update={"app_env": env})

    with pytest.raises(RuntimeError, match="seed-big запрещён"):
        await seed_big_database(settings, users=5)


@pytest.mark.parametrize(
    ("users", "message"),
    [(0, "положительным"), (-3, "положительным"), (BIG_MAX_USERS + 1, "не больше")],
)
async def test_the_number_of_people_must_be_sane(
    test_settings: Settings, users: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await seed_big_database(test_settings, users=users)


async def test_nothing_is_created_when_the_arguments_are_refused(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    with pytest.raises(RuntimeError):
        await seed_big_database(test_settings.model_copy(update={"app_env": "prod"}), users=20)
    with pytest.raises(ValueError, match="положительным"):
        await seed_big_database(test_settings, users=0)

    assert (await counts(admin_engine))["identity.users"] == 0


# ----------------------------------------------------------------------------- точка расширения
async def test_later_sprints_can_add_a_section_that_gets_the_whole_population(
    test_settings: Settings, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[BigPopulation] = []

    async def stub_posts(engine: AsyncEngine, population: BigPopulation) -> int:
        seen.append(population)
        assert engine is not None
        return 7

    monkeypatch.setattr(seeding, "BIG_SECTIONS", (("posts", stub_posts),))

    result = await seed_big_database(test_settings, users=60)

    assert result.extras == {"posts": 7}
    (population,) = seen
    assert (population.users, population.seed) == (60, BIG_DEFAULT_SEED)
    assert set(population.ids) == set(range(1, 61))
    stored = {
        row["u"]: row["id"]
        for row in await fetch_all(
            admin_engine, "SELECT id, username::text AS u FROM identity.users"
        )
    }
    assert {big_username(n): i for n, i in population.ids.items()} == stored
    private_in_db = {
        int(row["u"][4:])
        for row in await fetch_all(
            admin_engine,
            "SELECT u.username::text AS u FROM identity.users u "
            "JOIN profile.profiles p ON p.user_id = u.id WHERE p.is_private",
        )
    }
    assert private_in_db  # закрытые профили есть, и раздел о них знает
    assert population.private == private_in_db
    assert population.graph == build_big_graph(60, BIG_DEFAULT_SEED, private_in_db)


# ----------------------------------------------------------------------------- вход и работа с набором
async def test_seeded_people_log_in_and_the_graph_is_visible_through_the_api(
    test_settings: Settings, client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    await seed_big_database(test_settings, users=USERS)
    # Человек с друзьями: берём того, у кого их больше всего, и сверяем список с таблицей.
    best = await fetch_one(
        admin_engine,
        "SELECT u.username::text AS username, count(*) AS n FROM identity.users u JOIN "
        "(SELECT user_low_id AS a, user_high_id AS b FROM social.friendships UNION ALL "
        "SELECT user_high_id, user_low_id FROM social.friendships) f ON f.a = u.id "
        "WHERE u.status = 'active' GROUP BY u.username ORDER BY n DESC, u.username LIMIT 1",
    )

    login = await client.post(
        "/api/v1/auth/login", json={"login": best["username"], "password": SEED_PASSWORD}
    )

    assert login.status_code == 200, login.text
    headers = bearer(login.json()["access_token"])
    listed: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/v1/friends", params=params, headers=headers)).json()
        listed.extend(item["user"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(listed) == best["n"]
    assert len(set(listed)) == best["n"]
    profile = await client.get(f"/api/v1/users/{best['username']}", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["counters"]["friends"] == best["n"]
    me = (await client.get("/api/v1/me", headers=headers)).json()
    waiting = await fetch_one(
        admin_engine,
        "SELECT count(*) AS n FROM social.friend_requests r JOIN identity.users u ON u.id = r.sender_id "
        "WHERE r.status = 'pending' AND u.status = 'active' AND r.receiver_id = :id",
        id=uuid.UUID(me["id"]),
    )
    assert me["counters"]["pending_friend_requests"] == waiting["n"]
    # Поиск людей работает на этих данных (ник вида big_000NN находится по префиксу).
    found = await client.get(SEARCH, params={"q": "big_0000"}, headers=headers)
    assert found.status_code == 200
    assert all(
        item["user"]["username"].startswith("big_0000") for item in found.json()["items"][:5]
    )


# ----------------------------------------------------------------------------- командная строка
def test_the_cli_knows_the_seed_big_command() -> None:
    args = build_parser().parse_args(
        ["seed-big", "--users", "300", "--password", "x", "--seed", "9"]
    )
    assert (args.command, args.users, args.password, args.seed) == ("seed-big", 300, "x", 9)
    defaults = build_parser().parse_args(["seed-big"])
    assert (defaults.users, defaults.password, defaults.seed) == (
        BIG_DEFAULT_USERS,
        None,
        BIG_DEFAULT_SEED,
    )
    assert BIG_DEFAULT_USERS == 5000


async def test_the_seed_big_command_reports_what_it_did(
    test_settings: Settings,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("messunjerr.cli.get_settings", lambda: test_settings)

    code = await asyncio.to_thread(main, ["seed-big", "--users", "60"])

    out = capsys.readouterr().out
    assert code == 0
    assert "создано людей 60, уже было 0" in out
    for fragment in ("добавлено дружб", "подписок", "блокировок"):
        assert fragment in out
    assert "big_00001 … big_00060" in out
    assert SEED_PASSWORD in out
    assert (await counts(admin_engine))["identity.users"] == 60
    # Повтор: ничего не создано, и команда это говорит.
    again = await asyncio.to_thread(main, ["seed-big", "--users", "60"])
    assert again == 0
    assert "создано людей 0, уже было 60" in capsys.readouterr().out


@pytest.mark.parametrize("args", [[], ["--users", "0"]])
async def test_the_seed_big_command_fails_cleanly(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    refused = test_settings.model_copy(update={"app_env": "prod"} if not args else {})
    monkeypatch.setattr("messunjerr.cli.get_settings", lambda: refused)

    code = await asyncio.to_thread(main, ["seed-big", *args])

    captured = capsys.readouterr()
    assert code == 1
    assert "seed-big:" in captured.err
    assert ("seed-big запрещён" if not args else "положительным") in captured.err
    assert captured.out == ""


def test_the_result_type_reports_the_time() -> None:
    assert BigSeedResult(created=1, existing=0).seconds == 0.0

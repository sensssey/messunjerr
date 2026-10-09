"""`make seed-big` без базы (S8-07): имена, профили и граф связей строятся чистыми функциями.

Главное здесь инварианты графа: они же проверяет тест на настоящей БД после вставки, а тут их
проверяют на любых размерах и зёрнах (hypothesis), потому что вставка пропускает связь, только если
она не ломает инварианты S7, и эталоном для этого служит сам генератор.
"""

import collections
import random
import re
import statistics
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from messunjerr.core.me import (
    AUDIENCES,
    BIRTH_DATE_VISIBILITIES,
    LIST_VISIBILITIES,
    POST_VISIBILITIES,
)
from messunjerr.seeding import (
    BIG_DEFAULT_SEED,
    BIG_DEFAULT_USERS,
    BIG_HISTORY_DAYS,
    BIG_MAX_USERS,
    BIG_SECTIONS,
    FEMALE_NAMES,
    LATIN_ACCENTED,
    LATIN_FIRST_NAMES,
    LATIN_LAST_NAMES,
    MALE_NAMES,
    PATRONYMICS,
    SURNAMES,
    ZONES,
    BigGraph,
    big_display_name,
    big_email,
    big_is_private,
    big_number_of,
    big_privacy,
    big_profile,
    big_registered_at,
    big_username,
    build_big_graph,
    feminine_surname,
)

SEED = BIG_DEFAULT_SEED
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def private_of(users: int, seed: int = SEED) -> set[int]:
    return {number for number in range(1, users + 1) if big_is_private(number, seed)}


def unordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def assert_graph_invariants(graph: BigGraph, users: int, private: set[int]) -> None:
    """Всё, что обещает `BigGraph`: уникальность, порядок, самопары нет, блокировка исключает остальное."""
    numbers = range(1, users + 1)
    for edges in (
        graph.friendships,
        graph.follows,
        graph.blocks,
        graph.friend_requests,
        graph.follow_requests,
    ):
        assert all(e.first in numbers and e.second in numbers for e in edges)
        assert all(e.first != e.second for e in edges)  # самопар нет
        assert all(0.0 <= e.at < 1.0 for e in edges)
    friends = [unordered(e.first, e.second) for e in graph.friendships]
    assert all(
        e.first < e.second for e in graph.friendships
    )  # дружба хранится как «меньший, больший»
    assert len(set(friends)) == len(friends)
    follows = [(e.first, e.second) for e in graph.follows]
    assert len(set(follows)) == len(follows)
    blocks = [unordered(e.first, e.second) for e in graph.blocks]
    assert len(set(blocks)) == len(blocks)  # на пару не больше одной блокировки, в любую сторону
    pending_friend = [unordered(e.first, e.second) for e in graph.friend_requests]
    assert len(set(pending_friend)) == len(pending_friend)  # одна ждущая заявка на пару
    pending_follow = [(e.first, e.second) for e in graph.follow_requests]
    assert len(set(pending_follow)) == len(pending_follow)

    blocked = set(blocks)
    assert not blocked & set(friends)  # блокировка исключает дружбу
    assert not blocked & {unordered(a, b) for a, b in follows}  # и подписку в любую сторону
    assert not blocked & set(pending_friend)  # и ждущую заявку в друзья
    assert not blocked & {unordered(a, b) for a, b in pending_follow}  # и запрос на подписку
    assert not set(pending_friend) & set(friends)  # ждущая заявка исключает дружбу
    assert all(b in private for _, b in pending_follow)  # запрос только к закрытому профилю
    assert not set(pending_follow) & set(follows)  # и только от того, кто ещё не подписан


# ----------------------------------------------------------------------------- имена и ники
@pytest.mark.parametrize(
    ("masculine", "feminine"),
    [
        ("Иванов", "Иванова"),
        ("Петров", "Петрова"),
        ("Фёдоров", "Фёдорова"),
        ("Соловьёв", "Соловьёва"),
        ("Ёлкин", "Ёлкина"),
        ("Достоевский", "Достоевская"),
        ("Левицкий", "Левицкая"),
        ("Заболоцкий", "Заболоцкая"),
        ("Толстой", "Толстая"),
        ("Шевченко", "Шевченко"),
        ("Мельник", "Мельник"),
    ],
)
def test_feminine_surnames(masculine: str, feminine: str) -> None:
    assert feminine_surname(masculine) == feminine


def test_the_name_lists_are_clean_and_cover_the_letters_that_break_naive_search() -> None:
    for names in (MALE_NAMES, FEMALE_NAMES, SURNAMES, LATIN_FIRST_NAMES, LATIN_LAST_NAMES):
        assert len(set(names)) == len(names)
        assert all(name == name.strip() and name[0].isupper() for name in names)
    for names in (MALE_NAMES, FEMALE_NAMES, SURNAMES):
        assert all(re.fullmatch(r"[А-ЯЁа-яё]+", name) for name in names)
    assert any("ё" in name.lower() for name in MALE_NAMES)  # Пётр, Артём, Фёдор
    assert any("ё" in name.lower() for name in FEMALE_NAMES)  # Алёна
    assert sum("ё" in surname.lower() for surname in SURNAMES) >= 8
    assert all(not name.isascii() for name in LATIN_ACCENTED)
    assert all(a.endswith("ич") and b.endswith("на") for a, b in PATRONYMICS)


def test_usernames_and_emails_follow_the_numbering() -> None:
    assert big_username(1) == "big_00001"
    assert big_username(5000) == "big_05000"
    assert big_username(100_000) == "big_100000"
    assert big_email(42) == "big_00042@example.com"
    for number in (1, 42, 4999, 5000, 99_999, BIG_MAX_USERS):
        assert big_number_of(big_username(number)) == number
    for foreign in ("seed_0001", "big_", "big_x1", "BIG_00001", "big_٣", "bigfoot", "", "big_-1"):
        assert big_number_of(foreign) is None, foreign


def test_every_username_satisfies_the_database_format() -> None:
    pattern = re.compile(r"^[a-z0-9_]{3,30}$")
    assert all(pattern.fullmatch(big_username(n)) for n in (1, 5000, BIG_MAX_USERS))


def test_display_names_are_valid_and_varied() -> None:
    rng = random.Random(1)
    names = [big_display_name(rng) for _ in range(4000)]
    texts = [name for name, _ in names]

    assert all(1 <= len(name) <= 50 for name in texts)  # CHECK display_name_length
    assert all(name == name.strip() and "  " not in name for name in texts)
    latin_share = sum(is_latin for _, is_latin in names) / len(names)
    assert 0.05 < latin_share < 0.10
    assert all(not re.search(r"[А-Яа-яЁё]", name) for name, latin in names if latin)
    assert any(not name.isascii() for name, latin in names if latin)  # «Zoë Müller»
    assert all(re.search(r"[А-Яа-яЁё]", name) for name, latin in names if not latin)
    assert any("ё" in name.lower() for name in texts)
    assert any(name.islower() for name in texts)  # «анна иванова»
    assert any(name.isupper() for name in texts)  # «АННА ИВАНОВА»
    assert any(re.search(r"(ович|овна|евич|евна)\b", name) for name in texts)  # с отчеством
    assert any(" " not in name for name in texts)  # одно имя
    assert any(re.search(r"\s\w\.$", name) for name in texts)  # «Анна К.»
    assert any("-" in name for name in texts)  # двойная фамилия
    assert len(set(texts)) > 2500  # повторы естественны, но имена не одинаковые
    assert any(re.fullmatch(r"\w+ \w+ова", name) for name in texts)  # женские фамилии согласованы


# ----------------------------------------------------------------------------- профили
def test_a_profile_is_a_function_of_the_number_and_the_seed() -> None:
    assert big_profile(7, SEED) == big_profile(7, SEED)
    assert big_profile(7, SEED) != big_profile(8, SEED)
    differing = sum(big_profile(n, 1) != big_profile(n, 2) for n in range(1, 60))
    assert differing > 50


def test_profiles_fit_the_constraints_of_the_table() -> None:
    privates = 0
    for number in range(1, 2001):
        profile = big_profile(number, SEED)
        assert set(profile) == {
            "display_name",
            "bio",
            "links",
            "birth_date",
            "birth_date_visibility",
            "city",
            "language",
            "timezone",
            "is_private",
        }
        assert 1 <= len(profile["display_name"]) <= 50
        assert profile["bio"] is None or len(profile["bio"]) <= 500
        links: list[object] = profile["links"]
        assert isinstance(links, list)
        assert len(links) <= 5
        assert profile["birth_date_visibility"] in BIRTH_DATE_VISIBILITIES
        assert profile["city"] is None or len(profile["city"]) <= 100
        assert re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", profile["language"])
        assert profile["timezone"] in ZONES
        if profile["birth_date"] is not None:
            years = (NOW.date() - profile["birth_date"]).days / 365.25
            assert years >= 18  # возраст не меньше MIN_AGE
        assert profile["is_private"] is big_is_private(number, SEED)
        privates += profile["is_private"]
    assert 0.17 < privates / 2000 < 0.23  # около 20% закрытых


def test_privacy_settings_use_allowed_values_and_vary() -> None:
    seen: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for number in range(1, 1501):
        privacy = big_privacy(number, SEED)
        assert privacy == big_privacy(number, SEED)
        assert privacy["dm_policy"] in AUDIENCES
        assert privacy["comment_policy"] in AUDIENCES
        assert privacy["mention_policy"] in AUDIENCES
        assert privacy["presence_visibility"] in AUDIENCES
        assert privacy["friends_list_visibility"] in LIST_VISIBILITIES
        assert privacy["followers_list_visibility"] in LIST_VISIBILITIES
        assert privacy["default_post_visibility"] in POST_VISIBILITIES
        for key, value in privacy.items():
            seen[key][value] += 1
    # Видимость списков вразнобой: все три значения встречаются, ни одно не захватило всё.
    for key in ("friends_list_visibility", "followers_list_visibility"):
        assert set(seen[key]) == set(LIST_VISIBILITIES)
        assert max(seen[key].values()) < 0.6 * 1500


def test_registration_times_run_from_the_oldest_number_to_the_newest() -> None:
    first = big_registered_at(1, 5000, NOW)
    last = big_registered_at(5000, 5000, NOW)

    assert first == NOW - timedelta(days=BIG_HISTORY_DAYS)
    assert timedelta(0) < NOW - last < timedelta(days=1)
    stamps = [big_registered_at(n, 5000, NOW) for n in range(1, 5001)]
    assert stamps == sorted(set(stamps))  # возрастают строго


# ----------------------------------------------------------------------------- граф
@pytest.mark.parametrize("users", [0, 1, 2, 3, 5, 9, 10, 11, 25, 60, 300])
def test_the_graph_holds_its_invariants_at_every_size(users: int) -> None:
    private = private_of(users)

    graph = build_big_graph(users, SEED, private)

    assert_graph_invariants(graph, users, private)
    if users < 2:
        assert graph == BigGraph()


def test_the_graph_has_the_promised_shape_for_five_thousand_people() -> None:
    users = BIG_DEFAULT_USERS
    private = private_of(users)

    graph = build_big_graph(users, SEED, private)

    assert_graph_invariants(graph, users, private)
    assert len(graph.friendships) == 37_500  # в среднем 15 друзей на человека
    assert len(graph.follows) == 150_000  # в среднем 30 подписок
    assert len(graph.blocks) == 100  # небольшое число
    assert len(graph.friend_requests) == 2000
    assert 700 <= len(graph.follow_requests) <= 800  # около 0,8 на закрытый профиль
    degree = collections.Counter[int]()
    for edge in graph.friendships:
        degree[edge.first] += 1
        degree[edge.second] += 1
    degrees = [degree[n] for n in range(1, users + 1)]
    assert statistics.mean(degrees) == pytest.approx(15, abs=0.01)
    followers = collections.Counter(edge.second for edge in graph.follows)
    counts = sorted((followers[n] for n in range(1, users + 1)), reverse=True)
    assert statistics.mean(counts) == pytest.approx(30, abs=0.01)
    # Перекос в сторону «популярных»: у лучших тысячи подписчиков, у медианного человека в разы меньше.
    assert counts[0] > 1000
    assert counts[0] > 50 * statistics.median(counts)
    assert sum(counts[:50]) > 0.15 * len(graph.follows)
    private_followed = sum(1 for edge in graph.follows if edge.second in private)
    assert private_followed > 0  # на закрытые профили подписки уже одобрены


def test_the_same_input_gives_the_same_graph_and_another_seed_another_one() -> None:
    private = private_of(400)

    first = build_big_graph(400, SEED, private)
    again = build_big_graph(400, SEED, private)
    other = build_big_graph(400, SEED + 1, private)

    assert first == again
    assert first.friendships != other.friendships
    assert first.follows != other.follows


def test_every_kind_of_link_has_a_stream_of_its_own() -> None:
    """Список закрытых профилей влияет только на запросы на подписку: остальные связи не сдвигаются."""
    few = build_big_graph(300, SEED, {1, 2, 3})
    many = build_big_graph(300, SEED, set(range(1, 300, 2)))

    assert few.friendships == many.friendships
    assert few.follows == many.follows
    assert few.blocks == many.blocks
    assert few.friend_requests == many.friend_requests
    assert few.follow_requests != many.follow_requests


def test_without_closed_profiles_there_are_no_follow_requests() -> None:
    graph = build_big_graph(200, SEED, set())

    assert graph.follow_requests == ()
    assert_graph_invariants(graph, 200, set())


def test_numbers_outside_the_population_are_ignored_as_closed_profiles() -> None:
    graph = build_big_graph(50, SEED, {1, 2, 9999})

    assert_graph_invariants(graph, 50, {1, 2})
    assert all(e.second in {1, 2} for e in graph.follow_requests)


@settings(max_examples=60, deadline=None, database=None)
@given(
    users=st.integers(min_value=0, max_value=90),
    seed=st.integers(min_value=0, max_value=10_000),
    closed=st.sets(st.integers(min_value=1, max_value=90), max_size=40),
)
def test_the_invariants_hold_for_any_size_seed_and_closed_set(
    users: int, seed: int, closed: set[int]
) -> None:
    private = {number for number in closed if number <= users}

    graph = build_big_graph(users, seed, private)

    assert_graph_invariants(graph, users, private)
    assert graph == build_big_graph(users, seed, private)


def test_the_extension_point_for_later_sprints_is_empty_for_now() -> None:
    assert BIG_SECTIONS == ()  # S11 добавит сюда ("posts", seed_post_stubs)

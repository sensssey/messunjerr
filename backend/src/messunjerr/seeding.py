"""Тестовые данные для разработки: `make seed` (S3-07) и `make seed-big` (S8-07).

`seed` создаёт подтверждённые аккаунты `seed_0001`, `seed_0002`… с разными профилями: открытые и
закрытые, с датой рождения трёх уровней видимости, ссылками, городами и разной приватностью. Нужны,
чтобы было что смотреть через Swagger и `.http`-коллекции.

`seed-big` (внизу файла) строит большой набор `big_00001`… на 5 000 человек со связями: дружбы,
подписки с перекосом в сторону «популярных», блокировки, ждущие заявки и запросы на подписку.
На нём меряют поиск людей (S8), ленту (S11) и нагрузку (S19).

- Данные детерминированы: номер аккаунта (и зерно, у большого набора) определяет всё остальное,
  повторный запуск ничего не меняет (существующие ники пропускаются, связи не дублируются).
- У всех один пароль, поэтому хэш Argon2id считается один раз; это учебные данные, не боевые.
- В `prod` и `stage` команды отказываются работать.
"""

# Генератор берёт `random` для учебных данных, а не для секретов: замечание S311 здесь неуместно.
# ruff: noqa: S311

import itertools
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import TextClause, insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.db import create_engine
from messunjerr.core.ids import uuid7
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow
from messunjerr.settings import Settings
from messunjerr.social.domain.rules import ordered_pair
from messunjerr.tables import register_tables

SEED_PASSWORD = "seed-password-2026"  # noqa: S105 (общий пароль учебных аккаунтов, не секрет)
SEED_DOMAIN = "example.com"
BATCH_SIZE = 500

FIRST_NAMES = (
    "Анна", "Иван", "Мария", "Пётр", "Елена", "Дмитрий", "Ольга", "Сергей", "Наталья", "Алексей",
    "Татьяна", "Андрей", "Ирина", "Михаил", "Светлана", "Николай", "Юлия", "Владимир", "Екатерина",
    "Артём",
)  # fmt: skip
LAST_NAMES = (
    "Иванов", "Петрова", "Сидоров", "Кузнецова", "Смирнов", "Попова", "Васильев", "Соколова",
    "Михайлов", "Новикова", "Фёдоров", "Морозова", "Волков", "Алексеева", "Лебедев",
)  # fmt: skip
CITIES = ("Москва", "Казань", "Новосибирск", "Екатеринбург", "Самара", "Краснодар", "Тюмень")
ZONES = (
    "Europe/Moscow",
    "Europe/Moscow",
    "Asia/Novosibirsk",
    "Asia/Yekaterinburg",
    "Europe/Samara",
    "Europe/Moscow",
    "Asia/Yekaterinburg",
)
BIOS = (
    "Люблю горы и кофе",
    "Фотографирую рассветы",
    "Читаю про космос",
    "Бегаю по утрам",
    "Учу Python и немного Rust",
    "Собираю виниловые пластинки",
    None,
    "Путешествую, пока есть отпуск",
    None,
)
VISIBILITIES = ("hidden", "day_month", "full")


@dataclass(frozen=True, slots=True)
class SeedResult:
    created: int
    existing: int


def username_for(number: int) -> str:
    return f"seed_{number:04d}"


def email_for(number: int) -> str:
    return f"{username_for(number)}@{SEED_DOMAIN}"


def _profile_values(number: int, user_id: uuid.UUID) -> dict[str, Any]:
    """Профиль по номеру: каждый пятый закрыт, у двух из трёх есть дата рождения, и так далее."""
    has_birth_date = number % 3 != 0
    birth = date(1965 + number % 40, 1 + number % 12, 1 + number % 28)  # возраст не меньше 20 лет
    return {
        "user_id": user_id,
        "display_name": f"{FIRST_NAMES[number % len(FIRST_NAMES)]} {LAST_NAMES[number % len(LAST_NAMES)]}",
        "bio": BIOS[number % len(BIOS)],
        "links": (
            [{"title": "Блог", "url": f"https://example.com/{username_for(number)}"}]
            if number % 4 == 0
            else []
        ),
        "birth_date": birth if has_birth_date else None,
        "birth_date_visibility": VISIBILITIES[number % 3],
        "city": CITIES[number % len(CITIES)] if number % 6 != 0 else None,
        "language": "ru" if number % 10 else "en",
        "timezone": ZONES[number % len(ZONES)],
        "is_private": number % 5 == 0,
    }


def _privacy_values(number: int, user_id: uuid.UUID) -> dict[str, Any]:
    """Настройки приватности: по умолчанию, но у части людей иначе. Ключи у всех строк одни и те же:
    так их можно вставить одним пакетом."""
    open_lists = number % 7 == 0
    return {
        "user_id": user_id,
        "dm_policy": "everyone" if number % 4 == 0 else "friends",
        "comment_policy": "friends" if number % 11 == 0 else "everyone",
        "mention_policy": "everyone",
        "friends_list_visibility": "everyone" if open_lists else "friends",
        "followers_list_visibility": "everyone" if open_lists else "friends",
        "presence_visibility": "nobody" if number % 9 == 0 else "friends",
        "default_post_visibility": "friends",
    }


async def _seed_batch(
    engine: AsyncEngine,
    numbers: range,
    *,
    password_hash: str,
    settings: Settings,
    now: datetime,
) -> int:
    rows = [
        {
            "id": uuid7(),
            "email": email_for(number),
            "email_verified_at": now,
            "username": username_for(number),
            "terms_version": settings.legal_terms_version,
            "terms_accepted_at": now,
            "password_hash": password_hash,
            "status": "active",
        }
        for number in numbers
    ]
    async with engine.begin() as connection:
        created = (
            await connection.execute(
                pg_insert(UserRow)
                .values(rows)
                .on_conflict_do_nothing()
                .returning(UserRow.id, UserRow.username)
            )
        ).all()
        if created:
            by_number = {username_for(number): number for number in numbers}
            await connection.execute(
                insert(ProfileRow),
                [_profile_values(by_number[row.username], row.id) for row in created],
            )
            await connection.execute(
                insert(PrivacySettingsRow),
                [_privacy_values(by_number[row.username], row.id) for row in created],
            )
    return len(created)


async def seed_database(
    settings: Settings, *, users: int, password: str = SEED_PASSWORD
) -> SeedResult:
    """Создаёт до `users` учебных аккаунтов; уже существующие пропускает."""
    register_tables()  # внешний ключ аватара ссылается на media.assets: без неё не собрать порядок вставки
    if settings.strict_runtime:
        raise RuntimeError(f"seed запрещён при APP_ENV={settings.app_env}: это учебные данные")
    if users < 1:
        raise ValueError("число аккаунтов должно быть положительным")
    passwords = PasswordService.from_settings(settings)
    try:
        password_hash = await passwords.hash(password)
    finally:
        passwords.shutdown()
    engine = create_engine(settings)
    now = datetime.now(UTC)
    created = 0
    try:
        for start in range(1, users + 1, BATCH_SIZE):
            numbers = range(start, min(start + BATCH_SIZE, users + 1))
            created += await _seed_batch(
                engine, numbers, password_hash=password_hash, settings=settings, now=now
            )
    finally:
        await engine.dispose()
    return SeedResult(created=created, existing=users - created)


# ============================================================================= большой набор (S8-07)
# Чистые функции ниже (имена, профили, граф связей) не знают о базе: их проверяют unit-тесты, а вставку
# делает `seed_big_database`. У каждого раздела данных свой поток случайных чисел (`random.Random` от
# строки «зерно:раздел:…»): правка одного раздела не сдвигает остальные, а S11 добавит посты своим потоком.

BIG_PREFIX = "big_"
BIG_DEFAULT_USERS = 5000
BIG_MAX_USERS = 20_000
"""Граф строится в памяти (около 140 байт на связь: на 20 000 людей 750 тысяч связей и 250 МБ, на 100 000 уже
больше гигабайта), поэтому больше этого `seed-big` не берёт; растяжку для S19 начинать с этой границы."""
BIG_DEFAULT_SEED = 2026
BIG_PRIVATE_SHARE = 0.20
"""Доля закрытых профилей."""
BIG_FRIENDS_PER_PERSON = 15
"""Средняя степень в графе дружбы (каждая дружба считается у двоих)."""
BIG_FOLLOWS_PER_PERSON = 30
"""Подписок на человека в среднем."""
BIG_BLOCKS_PER_PERSON = 0.02
BIG_FRIEND_REQUESTS_PER_PERSON = 0.4
"""Ждущих заявок в друзья на человека."""
BIG_FOLLOW_REQUESTS_PER_PRIVATE = 0.8
"""Ждущих запросов на подписку на один закрытый профиль."""
BIG_HISTORY_DAYS = 500
"""Регистрации расставлены на столько дней назад: номер 1 самый старый, последний свежий."""
BIG_MAX_DENSITY = 0.3
"""Не больше такой доли возможных пар получает связь: иначе подбор уникальных пар застрянет."""

MALE_NAMES = (
    "Александр", "Алексей", "Анатолий", "Андрей", "Антон", "Аркадий", "Артём", "Артур", "Борис",
    "Богдан", "Вадим", "Валентин", "Валерий", "Василий", "Виктор", "Виталий", "Владимир",
    "Владислав", "Всеволод", "Вячеслав", "Геннадий", "Георгий", "Глеб", "Григорий", "Давид",
    "Даниил", "Денис", "Дмитрий", "Евгений", "Егор", "Ефим", "Захар", "Иван", "Игорь", "Илья",
    "Кирилл", "Константин", "Лев", "Леонид", "Макар", "Максим", "Марк", "Матвей", "Михаил",
    "Никита", "Николай", "Олег", "Павел", "Пётр", "Платон", "Роман", "Руслан", "Святослав",
    "Семён", "Сергей", "Станислав", "Степан", "Тимофей", "Тимур", "Тихон", "Фёдор", "Филипп",
    "Эдуард", "Юрий", "Ян", "Ярослав",
)  # fmt: skip
FEMALE_NAMES = (
    "Александра", "Алёна", "Алина", "Алиса", "Анастасия", "Анна", "Антонина", "Арина",
    "Валентина", "Валерия", "Варвара", "Вера", "Вероника", "Виктория", "Галина", "Дарья", "Диана",
    "Ева", "Евгения", "Екатерина", "Елена", "Елизавета", "Жанна", "Злата", "Инна", "Ирина", "Ия",
    "Кира", "Ксения", "Лариса", "Лидия", "Лилия", "Любовь", "Людмила", "Маргарита", "Марина",
    "Мария", "Милана", "Надежда", "Наталья", "Нина", "Оксана", "Олеся", "Ольга", "Полина",
    "Раиса", "Регина", "Светлана", "София", "Софья", "Таисия", "Тамара", "Татьяна", "Ульяна",
    "Эльвира", "Юлия", "Яна",
)  # fmt: skip
SURNAMES = (
    "Иванов", "Смирнов", "Кузнецов", "Попов", "Васильев", "Петров", "Соколов", "Михайлов",
    "Новиков", "Фёдоров", "Морозов", "Волков", "Алексеев", "Лебедев", "Семёнов", "Егоров",
    "Павлов", "Козлов", "Степанов", "Николаев", "Орлов", "Андреев", "Макаров", "Никитин",
    "Захаров", "Зайцев", "Соловьёв", "Борисов", "Яковлев", "Григорьев", "Романов", "Воробьёв",
    "Сергеев", "Кузьмин", "Фролов", "Александров", "Дмитриев", "Королёв", "Гусев", "Киселёв",
    "Ильин", "Максимов", "Поляков", "Сорокин", "Виноградов", "Ковалёв", "Белов", "Медведев",
    "Антонов", "Тарасов", "Жуков", "Баранов", "Филиппов", "Комаров", "Давыдов", "Беляев",
    "Герасимов", "Богданов", "Осипов", "Сидоров", "Матвеев", "Титов", "Марков", "Миронов",
    "Крылов", "Куликов", "Карпов", "Власов", "Мельников", "Денисов", "Гаврилов", "Тихонов",
    "Казаков", "Афанасьев", "Данилов", "Савельев", "Тимофеев", "Фомин", "Чернов", "Абрамов",
    "Мартынов", "Ефимов", "Щербаков", "Назаров", "Калинин", "Исаев", "Чернышёв", "Быков",
    "Маслов", "Родионов", "Коновалов", "Лазарев", "Воронин", "Климов", "Филатов", "Пономарёв",
    "Голубев", "Кудрявцев", "Прохоров", "Наумов", "Потапов", "Журавлёв", "Овчинников",
    "Трофимов", "Леонов", "Соболев", "Ермаков", "Колесников", "Гончаров", "Никифоров", "Грачёв",
    "Котов", "Гришин", "Ефремов", "Архипов", "Громов", "Кириллов", "Малышев", "Панов", "Моисеев",
    "Румянцев", "Акимов", "Кондратьев", "Бирюков", "Горбунов", "Анисимов", "Тихомиров", "Галкин",
    "Лукьянов", "Михеев", "Скворцов", "Юдин", "Белоусов", "Нестеров", "Симонов", "Харитонов",
    "Князев", "Цветков", "Левин", "Митрофанов", "Аксёнов", "Мальцев", "Логинов", "Горшков",
    "Савин", "Красильников", "Майоров", "Ёлкин", "Достоевский", "Чайковский", "Заболоцкий",
    "Левицкий", "Толстой", "Шевченко", "Бондаренко", "Ковальчук", "Мельник", "Ткаченко",
    "Лысенко", "Гайдай",
)  # fmt: skip
PATRONYMICS = (
    ("Иванович", "Ивановна"), ("Петрович", "Петровна"), ("Сергеевич", "Сергеевна"),
    ("Александрович", "Александровна"), ("Алексеевич", "Алексеевна"),
    ("Дмитриевич", "Дмитриевна"), ("Андреевич", "Андреевна"), ("Николаевич", "Николаевна"),
    ("Михайлович", "Михайловна"), ("Владимирович", "Владимировна"),
    ("Викторович", "Викторовна"), ("Олегович", "Олеговна"), ("Игоревич", "Игоревна"),
    ("Юрьевич", "Юрьевна"), ("Анатольевич", "Анатольевна"), ("Борисович", "Борисовна"),
    ("Евгеньевич", "Евгеньевна"), ("Павлович", "Павловна"), ("Максимович", "Максимовна"),
    ("Артёмович", "Артёмовна"), ("Фёдорович", "Фёдоровна"),
)  # fmt: skip
LATIN_FIRST_NAMES = (
    "John", "Mary", "James", "Emma", "David", "Sarah", "Michael", "Laura", "Daniel", "Julia",
    "Alex", "Kate", "Peter", "Olivia", "Thomas", "Sophie", "Robert", "Hannah", "Anton", "Elena",
    "Max", "Nina", "Oliver", "Grace", "Henry", "Alice", "Victor", "Irene", "Leo", "Clara",
)  # fmt: skip
LATIN_LAST_NAMES = (
    "Smith", "Johnson", "Brown", "Taylor", "Miller", "Wilson", "Moore", "Clark", "Lewis", "Walker",
    "Hall", "Allen", "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson",
    "Hill", "Campbell", "Mitchell", "Roberts", "Turner", "Parker", "Collins", "Stewart",
)  # fmt: skip
LATIN_ACCENTED = (
    "José García", "Zoë Müller", "Søren Larsen", "Renée Dubois", "Łukasz Nowak", "Noël Fournier",
    "Chloé Martin", "Jürgen Weber", "Inès Moreau", "Zoé Lefèvre",
)  # fmt: skip

_MASCULINE_ENDINGS = ("ов", "ев", "ёв", "ин", "ын")
_VOWEL_GENDER_FORMS = (("ский", "ская"), ("цкий", "цкая"), ("ой", "ая"))


def feminine_surname(surname: str) -> str:
    """Женская форма фамилии: Иванов → Иванова, Достоевский → Достоевская, Шевченко без изменений."""
    for masculine, feminine in _VOWEL_GENDER_FORMS:
        if surname.endswith(masculine):
            return surname[: -len(masculine)] + feminine
    return surname + "а" if surname.endswith(_MASCULINE_ENDINGS) else surname


def big_username(number: int) -> str:
    return f"{BIG_PREFIX}{number:05d}"


def big_email(number: int) -> str:
    return f"{big_username(number)}@{SEED_DOMAIN}"


def big_number_of(username: str) -> int | None:
    """Номер по нику `big_00042`; `None`, если ник не из этого набора."""
    digits = username.removeprefix(BIG_PREFIX)
    if digits == username or not (digits.isascii() and digits.isdigit()):
        return None
    return int(digits)


def big_display_name(rng: random.Random) -> tuple[str, bool]:
    """Отображаемое имя и признак «латиницей». Формы: «Имя Фамилия», «Фамилия Имя», с отчеством,
    одно имя, «Имя Ф.», дефисные фамилии, а также строчные и прописные написания."""
    if rng.random() < 0.07:
        if rng.random() < 0.15:
            return rng.choice(LATIN_ACCENTED), True
        return f"{rng.choice(LATIN_FIRST_NAMES)} {rng.choice(LATIN_LAST_NAMES)}", True
    female = rng.random() < 0.5
    first = rng.choice(FEMALE_NAMES if female else MALE_NAMES)
    surname = rng.choice(SURNAMES)
    if rng.random() < 0.03:  # дефисная фамилия
        surname = f"{surname}-{rng.choice(SURNAMES)}"
        last = (
            "-".join(feminine_surname(part) for part in surname.split("-")) if female else surname
        )
    else:
        last = feminine_surname(surname) if female else surname
    kind = rng.random()
    if kind < 0.66:
        name = f"{first} {last}"
    elif kind < 0.78:
        name = f"{last} {first}"
    elif kind < 0.85:
        patronymic = rng.choice(PATRONYMICS)[1 if female else 0]
        name = f"{first} {patronymic} {last}"
    elif kind < 0.89:
        name = first
    elif kind < 0.94:
        name = f"{first} {last[0]}."
    elif kind < 0.98:
        name = f"{first} {last}".lower()
    else:
        name = f"{first} {last}".upper()
    return name, False


def big_is_private(number: int, seed: int) -> bool:
    return random.Random(f"{seed}:private:{number}").random() < BIG_PRIVATE_SHARE


def big_profile(number: int, seed: int) -> dict[str, Any]:
    """Значения строки `profile.profiles` (без `user_id` и времени) для человека с номером `number`."""
    rng = random.Random(f"{seed}:profile:{number}")
    display_name, latin = big_display_name(rng)
    city = rng.randrange(len(CITIES))
    has_birth_date = rng.random() < 0.7
    birth = date(1960 + rng.randrange(45), 1 + rng.randrange(12), 1 + rng.randrange(28))
    return {
        "display_name": display_name,
        "bio": rng.choice(BIOS),
        "links": (
            [{"title": "Блог", "url": f"https://example.com/{big_username(number)}"}]
            if rng.random() < 0.2
            else []
        ),
        "birth_date": birth if has_birth_date else None,
        "birth_date_visibility": rng.choice(VISIBILITIES),
        "city": CITIES[city] if rng.random() < 0.85 else None,
        "language": "en" if latin or rng.random() < 0.03 else "ru",
        "timezone": ZONES[city],
        "is_private": big_is_private(number, seed),
    }


def big_privacy(number: int, seed: int) -> dict[str, Any]:
    """Значения строки `profile.privacy_settings`: настройки видимости списков идут вразнобой."""
    rng = random.Random(f"{seed}:privacy:{number}")
    audiences = ("everyone", "friends", "nobody")
    lists = ("everyone", "friends", "only_me")
    return {
        "dm_policy": rng.choices(audiences, weights=(30, 55, 15))[0],
        "comment_policy": rng.choices(audiences, weights=(70, 25, 5))[0],
        "mention_policy": rng.choices(audiences, weights=(70, 25, 5))[0],
        "friends_list_visibility": rng.choices(lists, weights=(35, 45, 20))[0],
        "followers_list_visibility": rng.choices(lists, weights=(35, 45, 20))[0],
        "presence_visibility": rng.choices(audiences, weights=(25, 60, 15))[0],
        "default_post_visibility": rng.choices(
            ("public", "friends", "private"), weights=(30, 60, 10)
        )[0],
    }


def big_registered_at(number: int, users: int, now: datetime) -> datetime:
    """Когда «зарегистрирован» человек: номера идут от самого старого к самому свежему."""
    return now - timedelta(days=BIG_HISTORY_DAYS) * (1 - (number - 1) / users)


class Edge(NamedTuple):
    """Связь двух людей по номерам; `at` (0..1) говорит, насколько близко к «сейчас» она возникла:
    между появлением позже пришедшего из двоих (0) и текущим моментом (1)."""

    first: int
    second: int
    at: float


@dataclass(frozen=True, slots=True)
class BigGraph:
    """Связи большого набора по номерам людей. Инварианты (проверяют unit-тесты и тест БД):

    - ни одна связь не соединяет человека с самим собой, пара не повторяется;
    - дружба хранится как `first < second`;
    - блокировка исключает дружбу, подписку в любую сторону и ждущие заявки и запросы между теми же людьми;
    - ждущая заявка в друзья исключает дружбу, ждущая заявка на пару одна;
    - запрос на подписку идёт только к закрытому профилю и только от того, кто ещё не подписан.
    """

    friendships: tuple[Edge, ...] = ()
    follows: tuple[Edge, ...] = ()
    blocks: tuple[Edge, ...] = ()
    friend_requests: tuple[Edge, ...] = ()
    follow_requests: tuple[Edge, ...] = ()


@dataclass(frozen=True, slots=True)
class _Sampler:
    """Выбор человека: равномерный либо с весами (накопленная сумма)."""

    population: Sequence[int]
    cumulative: Sequence[float] | None = None

    def draw(self, rng: random.Random, count: int) -> list[int]:
        return rng.choices(self.population, cum_weights=self.cumulative, k=count)


_MAX_ROUNDS = 40


def _draw_edges(
    rng: random.Random,
    target: int,
    first: _Sampler,
    second: _Sampler,
    *,
    stride: int,
    unordered: bool,
    keep_order: bool,
    taken: set[int],
    refuse: Callable[[int, int], bool],
) -> list[Edge]:
    """До `target` уникальных связей. `unordered`: пара считается одной в обе стороны; `keep_order`:
    связь хранит направление (иначе пара записывается как «меньший, больший»). `taken` хранит ключи
    принятых пар и пополняется; `refuse` отсекает пары, которые запрещают другие связи."""
    edges: list[Edge] = []
    for _ in range(_MAX_ROUNDS):
        need = target - len(edges)
        if need <= 0:
            break
        firsts = first.draw(rng, need * 2)
        seconds = second.draw(rng, need * 2)
        for a, b in zip(firsts, seconds, strict=True):
            if a == b:
                continue
            low, high = (b, a) if b < a else (a, b)
            key = low * stride + high if unordered else a * stride + b
            if key in taken or refuse(a, b):
                continue
            taken.add(key)
            edges.append(Edge(a, b, rng.random()) if keep_order else Edge(low, high, rng.random()))
            if len(edges) == target:
                break
    return edges


def _cumulative(weights: Sequence[float]) -> list[float]:
    return list(itertools.accumulate(weights))


def build_big_graph(users: int, seed: int, private: Collection[int]) -> BigGraph:
    """Связи для людей `1..users`; `private` номера закрытых профилей. Один и тот же вход даёт один и тот
    же граф; если людей слишком мало для нужной плотности, связей получается меньше.

    Дружба идёт почти равномерно, подписки с перекосом (закон Ципфа: несколько «популярных» аккаунтов
    собирают тысячи подписчиков), блокировки редкие и выбираются первыми, чтобы остальные связи их
    обходили.
    """
    if users < 2:
        return BigGraph()
    numbers = list(range(1, users + 1))
    stride = users + 1
    uniform = _Sampler(numbers)
    pairs = users * (users - 1) // 2

    popularity_rng = random.Random(f"{seed}:popularity")
    ranked = numbers[:]
    popularity_rng.shuffle(ranked)
    place = {number: rank for rank, number in enumerate(ranked)}
    popularity = [1.0 / (place[number] + 5) ** 0.85 for number in numbers]
    popular = _Sampler(numbers, _cumulative(popularity))
    activity_rng = random.Random(f"{seed}:activity")
    sociable = _Sampler(
        numbers, _cumulative([activity_rng.lognormvariate(0, 0.6) for _ in numbers])
    )

    def unordered_key(a: int, b: int) -> int:
        return (b * stride + a) if b < a else (a * stride + b)

    block_keys: set[int] = set()
    count = max(1, round(users * BIG_BLOCKS_PER_PERSON)) if users >= 10 else 0
    blocks = _draw_edges(
        random.Random(f"{seed}:blocks"),
        min(count, int(pairs * BIG_MAX_DENSITY)),
        uniform,
        uniform,
        stride=stride,
        unordered=True,
        keep_order=True,
        taken=block_keys,
        refuse=lambda a, b: False,
    )

    friend_keys: set[int] = set()
    friendships = _draw_edges(
        random.Random(f"{seed}:friendships"),
        min(round(users * BIG_FRIENDS_PER_PERSON / 2), int(pairs * BIG_MAX_DENSITY)),
        sociable,
        sociable,
        stride=stride,
        unordered=True,
        keep_order=False,
        taken=friend_keys,
        refuse=lambda a, b: unordered_key(a, b) in block_keys,
    )

    follow_keys: set[int] = set()
    follows = _draw_edges(
        random.Random(f"{seed}:follows"),
        min(users * BIG_FOLLOWS_PER_PERSON, int(users * (users - 1) * BIG_MAX_DENSITY)),
        uniform,
        popular,
        stride=stride,
        unordered=False,
        keep_order=True,
        taken=follow_keys,
        refuse=lambda a, b: unordered_key(a, b) in block_keys,
    )

    request_keys: set[int] = set()
    count = max(1, round(users * BIG_FRIEND_REQUESTS_PER_PERSON)) if users >= 10 else 0
    friend_requests = _draw_edges(
        random.Random(f"{seed}:friend-requests"),
        min(count, int(pairs * BIG_MAX_DENSITY)),
        uniform,
        sociable,
        stride=stride,
        unordered=True,
        keep_order=True,
        taken=request_keys,
        refuse=lambda a, b: unordered_key(a, b) in block_keys or unordered_key(a, b) in friend_keys,
    )

    closed = sorted(number for number in private if 1 <= number <= users)
    follow_request_keys: set[int] = set()
    follow_requests: list[Edge] = []
    if closed:
        closed_popular = _Sampler(
            closed, _cumulative([popularity[number - 1] for number in closed])
        )
        follow_requests = _draw_edges(
            random.Random(f"{seed}:follow-requests"),
            min(
                max(1, round(len(closed) * BIG_FOLLOW_REQUESTS_PER_PRIVATE)),
                int(len(closed) * (users - 1) * BIG_MAX_DENSITY),
            ),
            uniform,
            closed_popular,
            stride=stride,
            unordered=False,
            keep_order=True,
            taken=follow_request_keys,
            refuse=lambda a, b: (
                unordered_key(a, b) in block_keys
                or unordered_key(a, b) in friend_keys
                or a * stride + b in follow_keys
            ),
        )
    return BigGraph(
        friendships=tuple(friendships),
        follows=tuple(follows),
        blocks=tuple(blocks),
        friend_requests=tuple(friend_requests),
        follow_requests=tuple(follow_requests),
    )


@dataclass(frozen=True, slots=True)
class BigPopulation:
    """Что получает раздел, который расширяет набор (см. `BIG_SECTIONS`): люди, граф и «сейчас»."""

    users: int
    seed: int
    now: datetime
    ids: Mapping[int, uuid.UUID]
    """Номер человека → идентификатор в базе (номера `1..users`)."""
    private: frozenset[int]
    graph: BigGraph


type BigSection = Callable[[AsyncEngine, BigPopulation], Awaitable[int]]
"""Раздел данных: делает вставки отдельными транзакциями и возвращает, сколько строк создал."""

BIG_SECTIONS: tuple[tuple[str, BigSection], ...] = ()
"""Точка расширения: разделы, которые выполняются после людей и связей.

S11 (посты) добавит сюда `("posts", seed_post_stubs)`: раздел берёт `BigPopulation` (кто дружит, кто
подписан, кто закрыт) и пишет заготовки постов своим потоком случайных чисел
`random.Random(f"{population.seed}:posts")`. Повтор обязан быть безопасным, как у остальных разделов."""


@dataclass(frozen=True, slots=True)
class BigSeedResult:
    created: int
    """Новых людей."""
    existing: int
    friendships: int = 0
    follows: int = 0
    blocks: int = 0
    friend_requests: int = 0
    follow_requests: int = 0
    """Строки, которые этот запуск добавил (повтор даёт нули)."""
    extras: Mapping[str, int] = field(default_factory=dict[str, int])
    """Строки разделов из `BIG_SECTIONS` по их названиям."""
    seconds: float = 0.0


_EDGE_CHUNK = 20_000
"""Сколько связей уходит в одном запросе: три массива по 20 тысяч значений, запас до лимита asyncpg."""

_PAIR_IS_BLOCKED = (
    "EXISTS (SELECT 1 FROM social.blocks b WHERE LEAST(b.blocker_id, b.blocked_id) = LEAST(t.a, t.b) "
    "AND GREATEST(b.blocker_id, b.blocked_id) = GREATEST(t.a, t.b))"
)
_UNNEST = "unnest(CAST(:firsts AS uuid[]), CAST(:seconds AS uuid[]), CAST(:ats AS timestamptz[]))"

# Вставки с проверками: каждая связь добавляется, только если не нарушает инварианты S7–S8 с тем, что уже
# лежит в базе (прежний запуск с другим числом людей или зерном мог создать другие связи). В пустой базе
# проверки ничего не отсекают: граф уже согласован в памяти. Повтор: `ON CONFLICT DO NOTHING`.
_INSERT_BLOCKS = text(
    f"""
INSERT INTO social.blocks (blocker_id, blocked_id, created_at)
SELECT t.a, t.b, t.ts FROM {_UNNEST} AS t(a, b, ts)
WHERE NOT EXISTS (SELECT 1 FROM social.friendships f
                  WHERE f.user_low_id = LEAST(t.a, t.b) AND f.user_high_id = GREATEST(t.a, t.b))
  AND NOT EXISTS (SELECT 1 FROM social.follows s
                  WHERE (s.follower_id = t.a AND s.followee_id = t.b)
                     OR (s.follower_id = t.b AND s.followee_id = t.a))
  AND NOT EXISTS (SELECT 1 FROM social.friend_requests r
                  WHERE r.status = 'pending' AND LEAST(r.sender_id, r.receiver_id) = LEAST(t.a, t.b)
                    AND GREATEST(r.sender_id, r.receiver_id) = GREATEST(t.a, t.b))
  AND NOT EXISTS (SELECT 1 FROM social.follow_requests q
                  WHERE q.status = 'pending'
                    AND ((q.follower_id = t.a AND q.followee_id = t.b)
                      OR (q.follower_id = t.b AND q.followee_id = t.a)))
ON CONFLICT DO NOTHING
"""  # noqa: S608 (подставляются только константы этого модуля)
)
_INSERT_FRIENDSHIPS = text(
    f"""
INSERT INTO social.friendships (user_low_id, user_high_id, created_at)
SELECT t.a, t.b, t.ts FROM {_UNNEST} AS t(a, b, ts)
WHERE NOT {_PAIR_IS_BLOCKED}
  AND NOT EXISTS (SELECT 1 FROM social.friend_requests r
                  WHERE r.status = 'pending' AND LEAST(r.sender_id, r.receiver_id) = t.a
                    AND GREATEST(r.sender_id, r.receiver_id) = t.b)
ON CONFLICT DO NOTHING
"""  # noqa: S608
)
_INSERT_FOLLOWS = text(
    f"""
INSERT INTO social.follows (follower_id, followee_id, created_at)
SELECT t.a, t.b, t.ts FROM {_UNNEST} AS t(a, b, ts)
WHERE NOT {_PAIR_IS_BLOCKED}
  AND NOT EXISTS (SELECT 1 FROM social.follow_requests q
                  WHERE q.status = 'pending' AND q.follower_id = t.a AND q.followee_id = t.b)
ON CONFLICT DO NOTHING
"""  # noqa: S608
)
_INSERT_FRIEND_REQUESTS = text(
    f"""
INSERT INTO social.friend_requests (sender_id, receiver_id, status, created_at)
SELECT t.a, t.b, 'pending', t.ts FROM {_UNNEST} AS t(a, b, ts)
WHERE NOT {_PAIR_IS_BLOCKED}
  AND NOT EXISTS (SELECT 1 FROM social.friendships f
                  WHERE f.user_low_id = LEAST(t.a, t.b) AND f.user_high_id = GREATEST(t.a, t.b))
ON CONFLICT DO NOTHING
"""  # noqa: S608
)
_INSERT_FOLLOW_REQUESTS = text(
    f"""
INSERT INTO social.follow_requests (follower_id, followee_id, status, created_at)
SELECT t.a, t.b, 'pending', t.ts FROM {_UNNEST} AS t(a, b, ts)
WHERE NOT {_PAIR_IS_BLOCKED}
  AND NOT EXISTS (SELECT 1 FROM social.follows s WHERE s.follower_id = t.a AND s.followee_id = t.b)
  AND EXISTS (SELECT 1 FROM profile.profiles p WHERE p.user_id = t.b AND p.is_private)
ON CONFLICT DO NOTHING
"""  # noqa: S608
)


async def _insert_big_people(
    engine: AsyncEngine,
    numbers: range,
    *,
    seed: int,
    users: int,
    password_hash: str,
    settings: Settings,
    now: datetime,
) -> int:
    """Пачка людей одной транзакцией: аккаунты, профили и настройки приватности. Занятые ники пропускаются."""
    registered = {number: big_registered_at(number, users, now) for number in numbers}
    rows = [
        {
            "id": uuid7(),
            "email": big_email(number),
            "email_verified_at": registered[number],
            "username": big_username(number),
            "terms_version": settings.legal_terms_version,
            "terms_accepted_at": registered[number],
            "password_hash": password_hash,
            "status": "active",
            "created_at": registered[number],
            "updated_at": registered[number],
        }
        for number in numbers
    ]
    async with engine.begin() as connection:
        created = (
            await connection.execute(
                pg_insert(UserRow)
                .values(rows)
                .on_conflict_do_nothing()
                .returning(UserRow.id, UserRow.username)
            )
        ).all()
        if created:
            by_number = {row.username: big_number_of(row.username) or 0 for row in created}
            await connection.execute(
                insert(ProfileRow),
                [
                    {
                        "user_id": row.id,
                        **big_profile(by_number[row.username], seed),
                        "created_at": registered[by_number[row.username]],
                        "updated_at": registered[by_number[row.username]],
                    }
                    for row in created
                ],
            )
            await connection.execute(
                insert(PrivacySettingsRow),
                [
                    {"user_id": row.id, **big_privacy(by_number[row.username], seed)}
                    for row in created
                ],
            )
    return len(created)


async def _load_big_people(
    engine: AsyncEngine, users: int
) -> tuple[dict[int, uuid.UUID], frozenset[int]]:
    """Люди набора из базы (`1..users`): номер → идентификатор и номера закрытых профилей."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT u.id, u.username::text AS username, p.is_private "
                    "FROM identity.users u JOIN profile.profiles p ON p.user_id = u.id "
                    "WHERE u.username::text LIKE 'big\\_%'"
                )
            )
        ).all()
    ids: dict[int, uuid.UUID] = {}
    private: set[int] = set()
    for row in rows:
        number = big_number_of(row.username)
        if number is None or not 1 <= number <= users:
            continue
        ids[number] = row.id
        if row.is_private:
            private.add(number)
    return ids, frozenset(private)


@dataclass(frozen=True, slots=True)
class _EdgeWriter:
    """Переводит связи по номерам в строки таблиц и пишет их пачками по `_EDGE_CHUNK`."""

    engine: AsyncEngine
    ids: Mapping[int, uuid.UUID]
    registered: Sequence[datetime]
    now: datetime

    async def write(self, statement: TextClause, edges: Sequence[Edge], *, ordered: bool) -> int:
        """`ordered` ставит пару по порядку UUID (`low < high`, как у дружбы). Возвращает, сколько
        строк действительно добавлено (то, что уже было, и то, что запретила проверка, не считается)."""
        firsts: list[uuid.UUID] = []
        seconds: list[uuid.UUID] = []
        moments: list[datetime] = []
        for edge in edges:
            a, b = self.ids[edge.first], self.ids[edge.second]
            if ordered:
                a, b = ordered_pair(a, b)
            since = max(self.registered[edge.first - 1], self.registered[edge.second - 1])
            firsts.append(a)
            seconds.append(b)
            moments.append(since + (self.now - since) * edge.at)
        inserted = 0
        for start in range(0, len(firsts), _EDGE_CHUNK):
            end = start + _EDGE_CHUNK
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    statement,
                    {
                        "firsts": firsts[start:end],
                        "seconds": seconds[start:end],
                        "ats": moments[start:end],
                    },
                )
            inserted += result.rowcount
        return inserted


async def seed_big_database(
    settings: Settings,
    *,
    users: int = BIG_DEFAULT_USERS,
    password: str = SEED_PASSWORD,
    seed: int = BIG_DEFAULT_SEED,
    now: datetime | None = None,
) -> BigSeedResult:
    """Большой набор: `users` человек `big_00001`…, их связи и разделы из `BIG_SECTIONS`.

    Каждый шаг идёт отдельными транзакциями пачками, поэтому оборванный запуск просто повторяют.
    Повтор с теми же `users` и `seed` ничего не добавляет; с другими значениями добавляются недостающие
    люди и новые связи, а инварианты графа (блокировка исключает дружбу, подписку и ждущие заявки)
    сохраняются проверками в самих вставках.
    """
    register_tables()
    if settings.strict_runtime:
        raise RuntimeError(f"seed-big запрещён при APP_ENV={settings.app_env}: это учебные данные")
    if users < 1:
        raise ValueError("число аккаунтов должно быть положительным")
    if users > BIG_MAX_USERS:
        raise ValueError(f"число аккаунтов не больше {BIG_MAX_USERS}")
    started = time.perf_counter()
    moment = now or datetime.now(UTC)
    passwords = PasswordService.from_settings(settings)
    try:
        password_hash = await passwords.hash(password)
    finally:
        passwords.shutdown()
    engine = create_engine(settings)
    try:
        created = 0
        for start in range(1, users + 1, BATCH_SIZE):
            created += await _insert_big_people(
                engine,
                range(start, min(start + BATCH_SIZE, users + 1)),
                seed=seed,
                users=users,
                password_hash=password_hash,
                settings=settings,
                now=moment,
            )
        ids, private = await _load_big_people(engine, users)
        graph = build_big_graph(users, seed, private)
        registered = [big_registered_at(number, users, moment) for number in range(1, users + 1)]
        writer = _EdgeWriter(engine, ids, registered, moment)
        # Блокировки первыми: остальные связи проверяют себя против них.
        blocks = await writer.write(_INSERT_BLOCKS, graph.blocks, ordered=False)
        friendships = await writer.write(_INSERT_FRIENDSHIPS, graph.friendships, ordered=True)
        follows = await writer.write(_INSERT_FOLLOWS, graph.follows, ordered=False)
        friend_requests = await writer.write(
            _INSERT_FRIEND_REQUESTS, graph.friend_requests, ordered=False
        )
        follow_requests = await writer.write(
            _INSERT_FOLLOW_REQUESTS, graph.follow_requests, ordered=False
        )
        population = BigPopulation(
            users=users, seed=seed, now=moment, ids=ids, private=private, graph=graph
        )
        extras = {name: await section(engine, population) for name, section in BIG_SECTIONS}
    finally:
        await engine.dispose()
    return BigSeedResult(
        created=created,
        existing=users - created,
        friendships=friendships,
        follows=follows,
        blocks=blocks,
        friend_requests=friend_requests,
        follow_requests=follow_requests,
        extras=extras,
        seconds=time.perf_counter() - started,
    )

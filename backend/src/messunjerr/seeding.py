"""Тестовые данные для разработки: `make seed` (S3-07).

Создаёт подтверждённые аккаунты `seed_0001`, `seed_0002`… с разными профилями: открытые и закрытые,
с датой рождения трёх уровней видимости, ссылками, городами и разной приватностью. Нужны, чтобы было
что смотреть через Swagger и `.http`-коллекции; на этой базе в S7–S8 вырастут друзья и поиск людей
(в плане 5 000 человек).

- Данные детерминированы: номер аккаунта определяет всё остальное, повторный запуск ничего не меняет
  (существующие ники пропускаются).
- У всех один пароль, поэтому хэш Argon2id считается один раз; это учебные данные, не боевые.
- В `prod` и `stage` команда отказывается работать.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.db import create_engine
from messunjerr.core.ids import uuid7
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.media.infra import models as media_models
from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow
from messunjerr.settings import Settings

# Таблицы media регистрируются при импорте модуля: внешний ключ аватара ссылается на media.assets,
# и без неё SQLAlchemy не соберёт порядок вставки профилей.
REGISTERED_MODELS = (media_models.AssetRow,)

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

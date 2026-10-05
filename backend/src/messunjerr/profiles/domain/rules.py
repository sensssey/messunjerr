"""Правила полей профиля: возраст, дата рождения, ссылки (5.3). Чистые функции, без БД и часов."""

import unicodedata
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit

MAX_PLAUSIBLE_AGE = 120
"""Старше этого дату рождения считаем опечаткой, а не фактом."""
LINK_SCHEMES = frozenset({"http", "https"})


class BirthDateProblem(StrEnum):
    FUTURE = "future"
    UNDERAGE = "underage"
    IMPLAUSIBLE = "implausible"


def age_on(birth_date: date, today: date) -> int:
    """Полных лет на `today`; 29 февраля в невисокосный год «наступает» 1 марта."""
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def check_birth_date(birth_date: date, *, today: date, min_age: int) -> BirthDateProblem | None:
    """`None`, если дата подходит: не в будущем, возраст не меньше `MIN_AGE` и не выше разумного."""
    if birth_date > today:
        return BirthDateProblem.FUTURE
    age = age_on(birth_date, today)
    if age < min_age:
        return BirthDateProblem.UNDERAGE
    if age > MAX_PLAUSIBLE_AGE:
        return BirthDateProblem.IMPLAUSIBLE
    return None


def is_valid_link_url(url: str) -> bool:
    """Ссылка профиля: схема `http` или `https`, есть хост, нет логина с паролем и пробелов.

    `javascript:`, `data:`, `mailto:` и прочее отсекается схемой: клиент рисует ссылку как есть, и
    чужой текст не должен превращаться в исполняемый адрес.
    """
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in url):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # неверный порт даёт ValueError
    except ValueError:
        return False
    return (
        parts.scheme in LINK_SCHEMES
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
    )

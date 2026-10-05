"""Поля профиля, общие для регистрации (identity) и редактирования профиля (profiles).

Правила из 4.5 и 5.3: отображаемое имя 1–50 символов, язык по BCP 47, часовой пояс из базы IANA.
Ошибки используют коды элементов из каталога 5.14 (`invalid_format`), поэтому один и тот же ввод
даёт один и тот же ответ в обеих ручках.
"""

import re
from functools import cache
from typing import Annotated
from zoneinfo import available_timezones

from pydantic import AfterValidator, Field
from pydantic_core import PydanticCustomError

from messunjerr.core.codes import ItemCode
from messunjerr.core.limits import LIMITS

LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
LANGUAGE_MAX_LENGTH = 35
"""Длина, которую BCP 47 советует поддерживать (RFC 5646, 4.4.1)."""
TIMEZONE_MAX_LENGTH = 64

# Служебные имена каталога tz, которые не являются часовыми поясами людей.
_NOT_A_ZONE = frozenset({"localtime", "posixrules", "Factory"})


@cache
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones()) - _NOT_A_ZONE


def is_valid_timezone(value: str) -> bool:
    """Имя пояса из базы IANA; регистр важен (`Europe/Moscow`, а не `europe/moscow`)."""
    return value in _timezones()


def warm_up_timezones() -> int:
    """Читает базу поясов заранее и возвращает их число.

    Первый разбор сканирует около шестисот файлов (до 0,3 с): синхронная работа, которая не должна
    лечь на цикл событий вместе с первым запросом с часовым поясом. Приложение зовёт её при старте
    в потоке (`main.lifespan`).
    """
    return len(_timezones())


def canonical_language(value: str) -> str | None:
    """Приводит тег BCP 47 к каноническому написанию (`ru-ru` в `ru-RU`); `None`, если тег неверен.

    Первый подтег в нижнем регистре, двухбуквенный (регион) в верхнем, четырёхбуквенный (письменность)
    с заглавной буквы, остальное в нижнем. Результат всегда проходит проверку столбца в БД.
    """
    candidate = value.strip().replace("_", "-")
    if len(candidate) > LANGUAGE_MAX_LENGTH:
        return None
    first, *rest = candidate.split("-")
    canonical = "-".join(
        [
            first.lower(),
            *(
                part.upper()
                if len(part) == 2 and part.isalpha()
                else part.title()
                if len(part) == 4 and part.isalpha()
                else part.lower()
                for part in rest
            ),
        ]
    )
    return canonical if LANGUAGE_PATTERN.fullmatch(canonical) else None


def _valid_language(value: str) -> str:
    canonical = canonical_language(value)
    if canonical is None:
        raise PydanticCustomError(
            ItemCode.INVALID_FORMAT.value, "Use a BCP 47 language tag, for example ru or en-US."
        )
    return canonical


def _valid_timezone(value: str) -> str:
    if not is_valid_timezone(value):
        raise PydanticCustomError(
            ItemCode.INVALID_FORMAT.value, "Use an IANA time zone name, for example Europe/Moscow."
        )
    return value


DisplayName = Annotated[str, Field(min_length=1, max_length=LIMITS.display_name_max)]
"""Отображаемое имя: 1–50 символов после обрезки краёв."""

Language = Annotated[str, Field(max_length=LANGUAGE_MAX_LENGTH), AfterValidator(_valid_language)]
"""Язык интерфейса, BCP 47 (`ru`, `en-US`); сохраняется в каноническом написании."""

Timezone = Annotated[str, Field(max_length=TIMEZONE_MAX_LENGTH), AfterValidator(_valid_timezone)]
"""Часовой пояс человека, имя из базы IANA (`Europe/Moscow`)."""

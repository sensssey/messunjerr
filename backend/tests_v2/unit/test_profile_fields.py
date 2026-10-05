"""Поля профиля, общие для регистрации и редактирования: имя, язык, часовой пояс."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from messunjerr.core.fields import (
    DisplayName,
    Language,
    Timezone,
    canonical_language,
    is_valid_timezone,
)


class Probe(BaseModel):
    name: DisplayName | None = None
    language: Language | None = None
    timezone: Timezone | None = None


def error_types(**payload: Any) -> dict[str, str]:
    with pytest.raises(ValidationError) as caught:
        Probe.model_validate(payload)
    return {str(error["loc"][0]): error["type"] for error in caught.value.errors()}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("en-US", "en-US"),
        ("en-us", "en-US"),
        ("EN-us", "en-US"),
        ("ru_RU", "ru-RU"),
        ("zh-hans-cn", "zh-Hans-CN"),
        ("sr-Latn", "sr-Latn"),
        ("de-1996", "de-1996"),
        ("  ru  ", "ru"),
        ("fil", "fil"),
    ],
)
def test_language_tags_get_the_canonical_spelling(raw: str, expected: str) -> None:
    assert canonical_language(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "r", "russian", "ru-", "-ru", "ru--RU", "12", "ru-R", "ru-toolongsubtag", "ru RU", "ру"],
)
def test_malformed_language_tags_are_rejected(raw: str) -> None:
    assert canonical_language(raw) is None


def test_a_language_tag_is_capped_at_the_length_bcp47_recommends() -> None:
    assert canonical_language("en-" + "-".join(["abcd"] * 7)) is None


def test_canonical_tags_satisfy_the_database_check() -> None:
    from messunjerr.core.fields import LANGUAGE_PATTERN

    for raw in ("ru", "en-us", "zh-hans-cn", "sr_latn"):
        canonical = canonical_language(raw)
        assert canonical is not None
        assert LANGUAGE_PATTERN.fullmatch(canonical)


@pytest.mark.parametrize(
    "zone", ["Europe/Moscow", "Asia/Yekaterinburg", "UTC", "America/Argentina/Buenos_Aires"]
)
def test_iana_zones_are_valid(zone: str) -> None:
    assert is_valid_timezone(zone)


@pytest.mark.parametrize(
    "zone",
    [
        "",
        "europe/moscow",  # регистр важен
        "Moscow",
        "MSK",
        "UTC+3",
        "../../etc/passwd",
        "Europe/Moscow\n",
        "Mars/Olympus",
        "localtime",
        "posixrules",
        "Factory",
    ],
)
def test_other_strings_are_not_time_zones(zone: str) -> None:
    assert not is_valid_timezone(zone)


def test_the_schema_types_report_catalog_codes() -> None:
    assert error_types(language="russian") == {"language": "invalid_format"}
    assert error_types(timezone="Moscow") == {"timezone": "invalid_format"}
    assert error_types(name="") == {"name": "string_too_short"}
    assert error_types(name="я" * 51) == {"name": "string_too_long"}
    assert error_types(language="x" * 36) == {"language": "string_too_long"}


def test_the_schema_types_normalize_what_they_accept() -> None:
    probe = Probe.model_validate({"name": "Иван", "language": "en-us", "timezone": "Europe/Moscow"})
    assert (probe.name, probe.language, probe.timezone) == ("Иван", "en-US", "Europe/Moscow")


def test_warming_up_reads_the_zone_database_once() -> None:
    from messunjerr.core.fields import (
        _timezones,  # pyright: ignore[reportPrivateUsage]
        warm_up_timezones,
    )

    count = warm_up_timezones()

    assert count > 400
    assert _timezones.cache_info().currsize == 1
    hits = _timezones.cache_info().hits
    assert warm_up_timezones() == count  # повторный вызов берёт готовый набор, файлы не читаются
    assert _timezones.cache_info().hits == hits + 1

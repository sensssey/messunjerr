"""Мелкие утилиты ядра: UUIDv7, курсоры, нормализация строк и формат времени."""

import base64
import json
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from messunjerr.core.codes import ItemCode
from messunjerr.core.errors import InvalidCursorError
from messunjerr.core.ids import _uuid7_fallback, uuid7  # pyright: ignore[reportPrivateUsage]
from messunjerr.core.pagination import (
    Cursor,
    Page,
    decode_cursor,
    encode_cursor,
    split_page,
)
from messunjerr.core.schemas import RAW, ApiModel, UtcDateTime, normalize_text


# ----------------------------------------------------------------------------- UUIDv7
@pytest.mark.parametrize("factory", [uuid7, _uuid7_fallback], ids=["uuid7", "fallback"])
def test_uuid7_has_version_and_variant(factory: Any) -> None:
    value: uuid.UUID = factory()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


@pytest.mark.parametrize("factory", [uuid7, _uuid7_fallback], ids=["uuid7", "fallback"])
def test_uuid7_timestamp_prefix_is_monotonic(factory: Any) -> None:
    first: uuid.UUID = factory()
    time.sleep(0.003)
    second: uuid.UUID = factory()
    # Старшие 48 бит это время в миллисекундах: порядок по времени создания сохраняется.
    assert first.int >> 80 < second.int >> 80


def test_uuid7_timestamp_is_close_to_now() -> None:
    stamp_ms = _uuid7_fallback().int >> 80
    assert abs(stamp_ms - time.time() * 1000) < 5_000


# ----------------------------------------------------------------------------- курсоры
class FeedCursor(Cursor):
    created_at: datetime
    id: uuid.UUID


def _cursor() -> FeedCursor:
    return FeedCursor(created_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), id=uuid.UUID(int=7))


def test_cursor_round_trip() -> None:
    token = encode_cursor(_cursor())
    assert "=" not in token  # base64url без дополнения
    assert decode_cursor(token, FeedCursor) == _cursor()


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        "bm90LWpzb24",  # base64 от «not-json»
        "e30",  # {}: нет обязательных полей
        "eyJ2IjoyLCJpZCI6IngifQ",  # неверная версия формата
    ],
)
def test_broken_cursor_is_rejected(token: str) -> None:
    with pytest.raises(InvalidCursorError) as caught:
        decode_cursor(token, FeedCursor)
    assert caught.value.status == 400
    assert caught.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "stamp",
    [
        "9999-12-31T23:59:59-01:00",  # в UTC это уже год 10000
        "9999-12-31T23:59:59.999999-00:01",
        "0001-01-01T00:00:00+01:00",  # в UTC это год 0
        "0001-01-01T00:00:00+00:00:01",
    ],
)
def test_cursor_time_that_does_not_fit_the_database_is_invalid(stamp: str) -> None:
    """Драйвер падает на таком времени с `DataError` (500): курсор обязан отвечать `400`."""
    token = encode_json({"v": 1, "created_at": stamp, "id": str(uuid.UUID(int=7))})

    with pytest.raises(InvalidCursorError):
        decode_cursor(token, FeedCursor)


@pytest.mark.parametrize(
    "stamp",
    ["9999-12-31T23:59:59+00:00", "9999-12-31T22:59:59-01:00", "0001-01-01T01:00:00+01:00"],
)
def test_cursor_time_at_the_edge_that_does_fit_is_valid(stamp: str) -> None:
    token = encode_json({"v": 1, "created_at": stamp, "id": str(uuid.UUID(int=7))})

    assert decode_cursor(token, FeedCursor).created_at == datetime.fromisoformat(stamp)


def encode_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def test_cursor_of_another_list_is_rejected() -> None:
    class OtherCursor(Cursor):
        name: str

    token = encode_cursor(_cursor())
    with pytest.raises(InvalidCursorError):
        decode_cursor(token, OtherCursor)


def test_split_page_detects_next_page() -> None:
    assert split_page([1, 2, 3], 2) == ([1, 2], True)
    assert split_page([1, 2], 2) == ([1, 2], False)
    assert split_page([], 2) == ([], False)


def test_page_serializes_without_next_cursor() -> None:
    page = Page[int](items=[1, 2])
    assert page.model_dump() == {"items": [1, 2], "next_cursor": None}


# ----------------------------------------------------------------------------- строки
def test_normalize_text_trims_and_applies_nfc() -> None:
    decomposed = "Café "
    result = normalize_text(f"  {decomposed}")
    assert result == "Café"
    assert unicodedata.is_normalized("NFC", result)


def test_normalize_text_keeps_newline_and_tab() -> None:
    assert normalize_text("a\n\tb") == "a\n\tb"


@pytest.mark.parametrize("bad", ["a\x00b", "bell\x07", "esc\x1b[0m"])
def test_normalize_text_rejects_control_characters(bad: str) -> None:
    class Body(ApiModel):
        text: str

    with pytest.raises(ValidationError) as caught:
        Body(text=bad)
    assert caught.value.errors()[0]["type"] == ItemCode.INVALID_FORMAT.value


class _Body(ApiModel):
    title: str
    password: Any = None


def test_api_model_normalizes_strings() -> None:
    assert _Body(title="  Привет  ").title == "Привет"


def test_api_model_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"title": "x", "extra": 1})
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_raw_fields_are_not_touched() -> None:
    from typing import Annotated

    class Login(ApiModel):
        login: str
        password: Annotated[str, RAW]

    parsed = Login(login="  user ", password="  keep me  ")
    assert parsed.login == "user"
    assert parsed.password == "  keep me  "


# ----------------------------------------------------------------------------- время
class _Stamped(BaseModel):
    at: UtcDateTime


def test_utc_datetime_serializes_with_milliseconds_and_z() -> None:
    value = _Stamped(at=datetime(2026, 10, 4, 12, 34, 56, 789000, tzinfo=UTC))
    assert value.model_dump(mode="json") == {"at": "2026-10-04T12:34:56.789Z"}


def test_utc_datetime_converts_other_zones() -> None:
    moscow = timezone(timedelta(hours=3))
    value = _Stamped(at=datetime(2026, 10, 4, 15, 0, tzinfo=moscow))
    assert value.model_dump(mode="json") == {"at": "2026-10-04T12:00:00.000Z"}


def test_utc_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValidationError):
        _Stamped(at=datetime(2026, 10, 4, 12, 0))  # noqa: DTZ001 (проверяем именно наивное время)

"""Общие схемы API: базовая модель запроса, нормализация строк, формат времени (5.1)."""

import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PlainSerializer,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

from messunjerr.core.codes import ItemCode

_ALLOWED_CONTROLS = frozenset("\n\t")


def normalize_text(value: str) -> str:
    """Обрезает края, приводит к Unicode NFC, запрещает управляющие символы кроме `\\n` и `\\t`."""
    text = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROLS for ch in text):
        raise PydanticCustomError(
            ItemCode.INVALID_FORMAT.value, "Control characters are not allowed"
        )
    return text


def normalize_search(value: str | None) -> str | None:
    """Строка поиска из query-параметра: как у тел запросов (NFC, края обрезаны, управляющие
    символы запрещены, в том числе NUL, которого PostgreSQL не принимает); пустая строка значит
    «без поиска»."""
    if value is None:
        return None
    return normalize_text(value) or None


def _normalize_if_text(value: Any) -> Any:
    return normalize_text(value) if isinstance(value, str) else value


NormStr = Annotated[str, BeforeValidator(_normalize_if_text)]
"""Строка, нормализованная как требует 5.1 (используется внутри списков и словарей)."""


class _Raw:
    """Метка поля, значение которого нельзя менять (например, пароль): `Annotated[str, RAW]`."""


RAW = _Raw()


class ApiModel(BaseModel):
    """Базовая модель тел запросов: неизвестные поля запрещены, строки нормализуются."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_strings(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str) or info.field_name is None:
            return value
        metadata = cls.model_fields[info.field_name].metadata
        if any(isinstance(item, _Raw) for item in metadata):
            return value
        return normalize_text(value)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed, use UTC")
    return value


def format_utc(value: datetime) -> str:
    """RFC 3339 в UTC с миллисекундами и суффиксом `Z` (5.1)."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_require_aware),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]
"""RFC 3339, всегда UTC, миллисекунды, суффикс `Z`: `2026-10-04T12:34:56.789Z`."""

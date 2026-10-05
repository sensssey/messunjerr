"""Курсорная (keyset) пагинация: непрозрачные курсоры и страница `{items, next_cursor}` (5.1).

Курсор кодирует значения ключа сортировки (например, `created_at` и `id`) и версию формата.
Разбирать и собирать его клиенту нельзя; любая порча даёт `400 invalid_cursor`.
"""

import base64
import binascii
import json
from collections.abc import Sequence
from typing import Annotated, Literal

from fastapi import Query
from pydantic import BaseModel, ValidationError

from messunjerr.core.errors import InvalidCursorError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Размер страницы")]
CursorParam = Annotated[
    str | None, Query(max_length=512, description="Курсор из `next_cursor` предыдущего ответа")
]


class Cursor(BaseModel):
    """Базовый класс курсора: у каждого списка свой набор полей ключа сортировки."""

    v: Literal[1] = 1


class Page[T](BaseModel):
    """Ответ списка: элементы и курсор следующей страницы (`null` — конец списка)."""

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode_cursor[C: Cursor](token: str, model: type[C]) -> C:
    """Разбирает курсор; любая ошибка формата превращается в `InvalidCursorError`."""
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return model.model_validate(data)
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise InvalidCursorError from exc


def split_page[T](rows: Sequence[T], limit: int) -> tuple[list[T], bool]:
    """Запрос берёт `limit + 1` строк: лишняя означает, что следующая страница есть."""
    has_more = len(rows) > limit
    return list(rows[:limit]), has_more

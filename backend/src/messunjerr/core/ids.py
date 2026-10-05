"""Идентификаторы: UUIDv7 (сортируются по времени создания).

Сущности получают `id` от PostgreSQL (`DEFAULT uuidv7()` из PG 18); эта функция нужна там, где
идентификатор требуется до вставки: события outbox, `request_id`, ключи в тестах.
"""

import os
import sys
import time
import uuid


def _uuid7_fallback() -> uuid.UUID:
    """UUIDv7 по RFC 9562 для Python 3.13 (в 3.14 есть `uuid.uuid7`)."""
    timestamp_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7() -> uuid.UUID:
    if sys.version_info >= (3, 14):
        return uuid.uuid7()
    return _uuid7_fallback()

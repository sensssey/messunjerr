"""Время: везде UTC и aware-объекты."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)

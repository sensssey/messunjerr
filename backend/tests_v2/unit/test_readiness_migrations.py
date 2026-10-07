"""Готовность и миграции при выкладке без простоя (S4): БД «впереди» кода не снимает реплику."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.health import Readiness, check_migrations
from messunjerr.core.migrations import alembic_ini_path, expected_head, is_known_revision


class FakeResult:
    def __init__(self, revision: str | None) -> None:
        self._revision = revision

    def scalar_one_or_none(self) -> str | None:
        return self._revision


class FakeConnection:
    def __init__(self, revision: str | None, *, broken: bool) -> None:
        self._revision = revision
        self._broken = broken

    async def execute(self, _statement: Any) -> FakeResult:
        if self._broken:
            raise ConnectionError("БД недоступна")
        return FakeResult(self._revision)


class FakeEngine:
    def __init__(self, revision: str | None, *, broken: bool = False) -> None:
        self._revision = revision
        self._broken = broken

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[FakeConnection]:
        yield FakeConnection(self._revision, broken=self._broken)


def engine_at(revision: str | None, *, broken: bool = False) -> AsyncEngine:
    return cast(AsyncEngine, FakeEngine(revision, broken=broken))


def test_the_known_revisions_are_the_ones_next_to_the_code() -> None:
    head = expected_head()
    assert head is not None
    assert is_known_revision(head)
    assert is_known_revision("0001")
    assert not is_known_revision("9999_from_a_newer_release")


def test_without_migrations_next_to_the_code_nothing_is_known(tmp_path: Path) -> None:
    assert not is_known_revision("0001", tmp_path / "alembic.ini")
    assert alembic_ini_path().exists()


async def test_database_on_the_expected_revision_is_head() -> None:
    assert await check_migrations(engine_at("0003"), "0003") == "head"


async def test_a_known_but_not_latest_revision_is_behind() -> None:
    assert await check_migrations(engine_at("0002"), "0003") == "behind"


async def test_an_empty_version_table_is_behind() -> None:
    assert await check_migrations(engine_at(None), "0003") == "behind"


async def test_a_revision_this_code_does_not_know_is_ahead() -> None:
    # Старая реплика после миграции новым релизом, либо откат на старый код.
    assert await check_migrations(engine_at("9999_from_a_newer_release"), "0003") == "ahead"


async def test_without_expected_revision_or_database_the_answer_is_unknown() -> None:
    assert await check_migrations(engine_at("0003"), None) == "unknown"
    assert await check_migrations(engine_at("0003", broken=True), "0003") == "unknown"


@pytest.mark.parametrize(
    ("checks", "ready"),
    [
        ({"postgres": "ok", "redis": "ok", "migrations": "head"}, True),
        ({"postgres": "ok", "redis": "ok", "migrations": "ahead"}, True),
        ({"postgres": "ok", "redis": "ok", "migrations": "behind"}, False),
        ({"postgres": "ok", "redis": "ok", "migrations": "unknown"}, False),
        ({"postgres": "down", "redis": "ok", "migrations": "unknown"}, False),
        ({"postgres": "ok", "redis": "down", "migrations": "head"}, False),
        ({"shutdown": "draining"}, False),
    ],
)
def test_readiness_accepts_a_database_that_is_ahead_but_not_behind(
    checks: dict[str, str], ready: bool
) -> None:
    assert Readiness(checks=checks).ready is ready

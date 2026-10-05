"""Unit of Work и outbox на настоящем PostgreSQL: атомарность, откат и after-commit hooks."""

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.models import OutboxRow
from messunjerr.core.uow import UnitOfWork


class BoomError(Exception):
    pass


async def fail_after_flush(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="E", payload={})
        await uow.session.flush()  # строка уже отправлена в БД, но не зафиксирована
        raise BoomError


async def outbox_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[OutboxRow]:
    async with sessionmaker() as session:
        return list((await session.execute(select(OutboxRow).order_by(OutboxRow.id))).scalars())


async def test_commit_persists_the_event(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with UnitOfWork(sessionmaker) as uow:
        row = uow.outbox.add(
            topic="mj.social.graph.v1",
            key="a:b",
            event_type="FriendRequestSent",
            payload={"request_id": "r1"},
        )
        await uow.commit()
    (stored,) = await outbox_rows(sessionmaker)
    assert stored.event_id == row.event_id
    assert stored.event_id.version == 7
    assert (stored.topic, stored.key, stored.event_type) == (
        "mj.social.graph.v1",
        "a:b",
        "FriendRequestSent",
    )
    assert stored.payload == {"request_id": "r1"}
    assert stored.published_at is None
    assert stored.attempts == 0
    assert stored.created_at is not None


async def test_exception_rolls_back_state_and_event_together(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(BoomError):
        await fail_after_flush(sessionmaker)
    assert await outbox_rows(sessionmaker) == []


async def test_leaving_without_commit_discards_changes(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="E", payload={})
    assert await outbox_rows(sessionmaker) == []


async def test_explicit_rollback_discards_events_and_hooks(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []

    async def hook() -> None:
        calls.append("called")

    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="E", payload={})
        uow.after_commit(hook)
        await uow.rollback()
        await uow.commit()  # после отката hooks уже сброшены
    assert calls == []
    assert await outbox_rows(sessionmaker) == []


async def test_after_commit_hooks_run_only_after_a_successful_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seen_rows_at_hook_time: list[int] = []

    async def hook() -> None:
        seen_rows_at_hook_time.append(len(await outbox_rows(sessionmaker)))

    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="E", payload={})
        uow.after_commit(hook)
        assert seen_rows_at_hook_time == []  # до коммита hook не вызывается
        await uow.commit()
    assert seen_rows_at_hook_time == [1]  # к моменту hook событие уже видно другим соединениям


async def test_failing_hook_does_not_undo_the_commit_or_stop_other_hooks(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []

    async def broken() -> None:
        raise RuntimeError("redis is down")

    async def healthy() -> None:
        calls.append("healthy")

    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="E", payload={})
        uow.after_commit(broken)
        uow.after_commit(healthy)
        await uow.commit()
    assert calls == ["healthy"]
    assert len(await outbox_rows(sessionmaker)) == 1


async def test_events_keep_insertion_order(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with UnitOfWork(sessionmaker) as uow:
        for number in range(5):
            uow.outbox.add(topic="t", key="k", event_type=f"E{number}", payload={"n": number})
        await uow.commit()
    rows = await outbox_rows(sessionmaker)
    assert [row.event_type for row in rows] == ["E0", "E1", "E2", "E3", "E4"]
    assert [row.id for row in rows] == sorted(row.id for row in rows)


async def test_request_id_travels_with_the_event(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    structlog.contextvars.bind_contextvars(request_id="req-42")
    try:
        async with UnitOfWork(sessionmaker) as uow:
            uow.outbox.add(topic="t", key="k", event_type="E", payload={}, headers={"x": 1})
            await uow.commit()
    finally:
        structlog.contextvars.clear_contextvars()
    (stored,) = await outbox_rows(sessionmaker)
    assert stored.headers == {"x": 1, "correlation_id": "req-42"}


async def test_unit_of_work_can_be_reused_after_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(sessionmaker) as uow:
        uow.outbox.add(topic="t", key="k", event_type="first", payload={})
        await uow.commit()
        uow.outbox.add(topic="t", key="k", event_type="second", payload={})
        await uow.commit()
    assert [row.event_type for row in await outbox_rows(sessionmaker)] == ["first", "second"]

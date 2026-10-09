"""Миграции 0006 (S7) и 0007 (S8): заявки в друзья, дружба, блокировки, подписки и запросы на подписку,
их ограничения, индексы и права."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.ids import uuid7
from messunjerr.settings import Settings
from messunjerr.social.domain.rules import ordered_pair

from .conftest import DatabaseUnderTest, create_database, drop_database, run_alembic
from .helpers import execute, fetch_all, fetch_one

COLUMNS = {
    "friend_requests": {"id", "sender_id", "receiver_id", "status", "created_at", "responded_at"},
    "friendships": {"user_low_id", "user_high_id", "created_at"},
    "blocks": {"blocker_id", "blocked_id", "created_at"},
}
FOLLOW_COLUMNS = {
    "follows": {"follower_id", "followee_id", "created_at"},
    "follow_requests": {"id", "follower_id", "followee_id", "status", "created_at", "responded_at"},
}
INSUFFICIENT_PRIVILEGE = "42501"


async def accounts(engine: AsyncEngine, count: int = 2) -> list[uuid.UUID]:
    """Аккаунты напрямую в БД, по возрастанию идентификатора (так хранится пара друзей)."""
    ids = sorted((uuid.uuid4() for _ in range(count)), key=lambda value: value.int)
    for index, account in enumerate(ids):
        await execute(
            engine,
            "INSERT INTO identity.users (id, email, username, terms_version, terms_accepted_at, status) "
            "VALUES (:id, :email, :username, 'v', now(), 'active')",
            id=account,
            email=f"user{index}-{account.hex[:8]}@example.com",
            username=f"user_{account.hex[:12]}",
        )
    return ids


async def counts(engine: AsyncEngine) -> dict[str, int]:
    found: dict[str, int] = {}
    for table in COLUMNS:
        row = await fetch_one(engine, f"SELECT count(*) AS n FROM social.{table}")
        found[table] = row["n"]
    return found


def request(sender: uuid.UUID, receiver: uuid.UUID, status: str = "pending") -> dict[str, Any]:
    return {"sender": sender, "receiver": receiver, "status": status}


INSERT_REQUEST = (
    "INSERT INTO social.friend_requests (sender_id, receiver_id, status) "
    "VALUES (:sender, :receiver, :status)"
)


async def test_the_tables_have_the_columns_of_the_specification(admin_engine: AsyncEngine) -> None:
    for table, expected in COLUMNS.items():
        rows = await fetch_all(
            admin_engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'social' AND table_name = :table",
            table=table,
        )
        assert {row["column_name"] for row in rows} == expected, table


async def test_indexes_serve_the_lists_and_keep_one_live_request_per_pair(
    admin_engine: AsyncEngine,
) -> None:
    rows = await fetch_all(
        admin_engine,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'social'",
    )
    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert {
        "pk_friend_requests", "pk_friendships", "pk_blocks", "ux_friend_requests_pending",
        "ix_friend_requests_receiver", "ix_friend_requests_sender", "ix_friendships_high",
        "ix_blocks_blocked",
    } <= set(definitions)  # fmt: skip
    pending = definitions["ux_friend_requests_pending"]
    assert pending.startswith("CREATE UNIQUE INDEX")
    assert "LEAST(sender_id, receiver_id)" in pending
    assert "GREATEST(sender_id, receiver_id)" in pending
    assert "status = 'pending'" in pending
    for name in ("ix_friend_requests_receiver", "ix_friend_requests_sender"):
        assert "status = 'pending'" in definitions[name]


async def test_a_new_request_gets_its_defaults(admin_engine: AsyncEngine) -> None:
    sender, receiver = await accounts(admin_engine)

    await execute(
        admin_engine,
        "INSERT INTO social.friend_requests (sender_id, receiver_id) VALUES (:sender, :receiver)",
        sender=sender,
        receiver=receiver,
    )

    row = await fetch_one(
        admin_engine, "SELECT id, status, created_at, responded_at FROM social.friend_requests"
    )
    assert row["id"].version == 7
    assert row["status"] == "pending"
    assert row["created_at"] is not None
    assert row["responded_at"] is None


async def test_the_database_refuses_nonsense_rows(admin_engine: AsyncEngine) -> None:
    low, high = await accounts(admin_engine)
    nonsense: list[tuple[str, dict[str, Any]]] = [
        (INSERT_REQUEST, request(low, low)),  # заявка самому себе
        (INSERT_REQUEST, request(low, high, "frozen")),  # неизвестный статус
        (INSERT_REQUEST, request(low, uuid.uuid4())),  # получателя нет
        (INSERT_REQUEST, request(uuid.uuid4(), high)),  # отправителя нет
        (
            "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
            {"a": high, "b": low},  # пара не по порядку: дружбу хранят один раз
        ),
        (
            "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
            {"a": low, "b": low},  # дружба с собой
        ),
        (
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            {"a": low, "b": low},  # блокировка себя
        ),
        (
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            {"a": low, "b": uuid.uuid4()},  # заблокированного нет
        ),
    ]
    for statement, values in nonsense:
        with pytest.raises(IntegrityError):
            await execute(admin_engine, statement, **values)
    assert await counts(admin_engine) == {"friend_requests": 0, "friendships": 0, "blocks": 0}


async def test_only_one_live_request_exists_for_a_pair_in_either_direction(
    admin_engine: AsyncEngine,
) -> None:
    first, second = await accounts(admin_engine)
    await execute(admin_engine, INSERT_REQUEST, **request(first, second))

    for sender, receiver in ((first, second), (second, first)):
        with pytest.raises(IntegrityError):
            await execute(admin_engine, INSERT_REQUEST, **request(sender, receiver))

    # Закрытые заявки не мешают: история копится, живая заявка снова возможна.
    await execute(admin_engine, "UPDATE social.friend_requests SET status = 'declined'")
    await execute(admin_engine, INSERT_REQUEST, **request(second, first))
    await execute(
        admin_engine,
        "UPDATE social.friend_requests SET status = 'cancelled' WHERE status = 'pending'",
    )
    await execute(admin_engine, INSERT_REQUEST, **request(first, second))
    await execute(admin_engine, INSERT_REQUEST, **request(second, first, "accepted"))
    rows = await fetch_all(
        admin_engine, "SELECT status FROM social.friend_requests ORDER BY created_at, id"
    )
    assert sorted(row["status"] for row in rows) == ["accepted", "cancelled", "declined", "pending"]


async def test_friendships_and_blocks_are_unique_and_a_pair_has_at_most_one_block(
    admin_engine: AsyncEngine,
) -> None:
    low, high = await accounts(admin_engine)
    await execute(
        admin_engine,
        "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
        a=low,
        b=high,
    )
    with pytest.raises(IntegrityError):
        await execute(
            admin_engine,
            "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
            a=low,
            b=high,
        )
    insert_block = "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)"
    await execute(admin_engine, insert_block, a=low, b=high)
    with pytest.raises(IntegrityError):  # то же направление: ключ строки
        await execute(admin_engine, insert_block, a=low, b=high)
    with pytest.raises(IntegrityError):  # встречное: взаимной блокировки не бывает (4.6)
        await execute(admin_engine, insert_block, a=high, b=low)
    assert (await counts(admin_engine))["blocks"] == 1
    # После снятия блокировки пара свободна, и блокирует уже другой.
    await execute(admin_engine, "DELETE FROM social.blocks")
    await execute(admin_engine, insert_block, a=high, b=low)
    assert (await counts(admin_engine))["blocks"] == 1


async def test_deleting_an_account_removes_everything_that_points_at_it(
    admin_engine: AsyncEngine,
) -> None:
    one, two, three = await accounts(admin_engine, 3)
    await execute(admin_engine, INSERT_REQUEST, **request(one, two))
    await execute(admin_engine, INSERT_REQUEST, **request(three, one, "declined"))
    await execute(admin_engine, INSERT_REQUEST, **request(two, three))
    for first, second in ((one, two), (two, three)):
        await execute(
            admin_engine,
            "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
            a=first,
            b=second,
        )
    for blocker, blocked in ((one, two), (three, one)):
        await execute(
            admin_engine,
            "INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)",
            a=blocker,
            b=blocked,
        )

    await execute(admin_engine, "DELETE FROM identity.users WHERE id = :id", id=one)

    assert await counts(admin_engine) == {"friend_requests": 1, "friendships": 1, "blocks": 0}
    left = await fetch_one(
        admin_engine, "SELECT sender_id, receiver_id FROM social.friend_requests"
    )
    assert (left["sender_id"], left["receiver_id"]) == (two, three)


async def test_the_app_role_works_with_the_graph_and_the_readonly_role_only_reads(
    engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    low, high = await accounts(admin_engine)
    async with engine.begin() as connection:  # роль app: полный DML
        await connection.execute(text(INSERT_REQUEST), request(low, high))
        await connection.execute(text("UPDATE social.friend_requests SET status = 'accepted'"))
        await connection.execute(
            text("INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)"),
            {"a": low, "b": high},
        )
        await connection.execute(
            text("INSERT INTO social.blocks (blocker_id, blocked_id) VALUES (:a, :b)"),
            {"a": low, "b": high},
        )
        for table in COLUMNS:
            await connection.execute(text(f"DELETE FROM social.{table}"))

    async def as_readonly(statement: str) -> None:
        async with admin_engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE readonly"))
            await connection.execute(text(statement))

    for table in COLUMNS:
        await as_readonly(f"SELECT * FROM social.{table}")
        with pytest.raises(ProgrammingError) as caught:
            await as_readonly(f"DELETE FROM social.{table}")
        assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, table


# ----------------------------------------------------------------------------- подписки (0007, S8)
INSERT_FOLLOW = "INSERT INTO social.follows (follower_id, followee_id) VALUES (:a, :b)"
INSERT_FOLLOW_REQUEST = (
    "INSERT INTO social.follow_requests (follower_id, followee_id, status) VALUES (:a, :b, :status)"
)


async def follow_counts(engine: AsyncEngine) -> dict[str, int]:
    found: dict[str, int] = {}
    for table in FOLLOW_COLUMNS:
        row = await fetch_one(engine, f"SELECT count(*) AS n FROM social.{table}")
        found[table] = row["n"]
    return found


def follow_request(a: uuid.UUID, b: uuid.UUID, status: str = "pending") -> dict[str, Any]:
    return {"a": a, "b": b, "status": status}


async def test_the_follow_tables_have_the_columns_of_the_specification(
    admin_engine: AsyncEngine,
) -> None:
    for table, expected in FOLLOW_COLUMNS.items():
        rows = await fetch_all(
            admin_engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'social' AND table_name = :table",
            table=table,
        )
        assert {row["column_name"] for row in rows} == expected, table
    nullable = await fetch_all(
        admin_engine,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'social' AND table_name IN ('follows', 'follow_requests') "
        "AND is_nullable = 'YES'",
    )
    assert [(row["table_name"], row["column_name"]) for row in nullable] == [
        ("follow_requests", "responded_at")
    ]  # необязателен один столбец


async def test_follow_indexes_serve_both_lists_and_keep_one_live_request_per_direction(
    admin_engine: AsyncEngine,
) -> None:
    rows = await fetch_all(
        admin_engine,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'social' "
        "AND tablename IN ('follows', 'follow_requests')",
    )
    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert {
        "pk_follows",
        "ix_follows_follower",
        "ix_follows_followee",
        "pk_follow_requests",
        "ux_follow_requests_pending",
        "ix_follow_requests_followee",
    } <= set(definitions)
    # «На кого я подписан» и «кто подписан на меня» читаются по времени подписки.
    assert "(follower_id, created_at)" in definitions["ix_follows_follower"]
    assert "(followee_id, created_at)" in definitions["ix_follows_followee"]
    pending = definitions["ux_follow_requests_pending"]
    assert pending.startswith("CREATE UNIQUE INDEX")
    assert "(follower_id, followee_id)" in pending  # направление важно: встречные запросы возможны
    assert "status = 'pending'" in pending
    waiting = definitions["ix_follow_requests_followee"]
    assert "(followee_id, created_at)" in waiting
    assert "status = 'pending'" in waiting


async def test_the_follow_constraints_carry_the_names_of_the_convention(
    admin_engine: AsyncEngine,
) -> None:
    rows = await fetch_all(
        admin_engine,
        "SELECT conrelid::regclass::text AS table_name, conname FROM pg_constraint "
        "WHERE conrelid IN ('social.follows'::regclass, 'social.follow_requests'::regclass) "
        "AND contype <> 'n'",  # PostgreSQL 18 хранит и NOT NULL как ограничения: они без наших имён
    )
    names = {(row["table_name"], row["conname"]) for row in rows}
    assert names == {
        ("social.follows", "pk_follows"),
        ("social.follows", "ck_follows_distinct_users"),
        ("social.follows", "fk_follows_follower_id_users"),
        ("social.follows", "fk_follows_followee_id_users"),
        ("social.follow_requests", "pk_follow_requests"),
        ("social.follow_requests", "ck_follow_requests_status"),
        ("social.follow_requests", "ck_follow_requests_distinct_users"),
        ("social.follow_requests", "fk_follow_requests_follower_id_users"),
        ("social.follow_requests", "fk_follow_requests_followee_id_users"),
    }


async def test_a_new_follow_request_and_a_new_follow_get_their_defaults(
    admin_engine: AsyncEngine,
) -> None:
    follower, followee = await accounts(admin_engine)

    await execute(
        admin_engine,
        "INSERT INTO social.follow_requests (follower_id, followee_id) VALUES (:a, :b)",
        a=follower,
        b=followee,
    )
    await execute(admin_engine, INSERT_FOLLOW, a=follower, b=followee)

    row = await fetch_one(
        admin_engine, "SELECT id, status, created_at, responded_at FROM social.follow_requests"
    )
    assert row["id"].version == 7
    assert row["status"] == "pending"
    assert row["created_at"] is not None
    assert row["responded_at"] is None
    assert (await fetch_one(admin_engine, "SELECT created_at FROM social.follows"))[
        "created_at"
    ] is not None


async def test_the_database_refuses_nonsense_follow_rows(admin_engine: AsyncEngine) -> None:
    first, second = await accounts(admin_engine)
    await execute(admin_engine, INSERT_FOLLOW, a=first, b=second)
    nonsense: list[tuple[str, dict[str, Any]]] = [
        (INSERT_FOLLOW, {"a": first, "b": first}),  # подписка на себя
        (INSERT_FOLLOW, {"a": first, "b": second}),  # вторая такая же подписка
        (INSERT_FOLLOW, {"a": first, "b": uuid.uuid4()}),  # подписываться не на кого
        (INSERT_FOLLOW, {"a": uuid.uuid4(), "b": second}),  # подписчика нет
        (INSERT_FOLLOW_REQUEST, follow_request(first, first)),  # запрос самому себе
        (INSERT_FOLLOW_REQUEST, follow_request(first, second, "frozen")),  # неизвестный статус
        (
            INSERT_FOLLOW_REQUEST,
            follow_request(first, second, "accepted"),
        ),  # статус заявок в друзья
        (INSERT_FOLLOW_REQUEST, follow_request(first, uuid.uuid4())),
        (INSERT_FOLLOW_REQUEST, follow_request(uuid.uuid4(), second)),
    ]
    for statement, values in nonsense:
        with pytest.raises(IntegrityError):
            await execute(admin_engine, statement, **values)
    assert await follow_counts(admin_engine) == {"follows": 1, "follow_requests": 0}


async def test_a_pair_may_follow_each_other_but_only_one_waiting_request_exists_per_direction(
    admin_engine: AsyncEngine,
) -> None:
    first, second = await accounts(admin_engine)
    # Подписки друг на друга независимы: у каждой направление своё.
    await execute(admin_engine, INSERT_FOLLOW, a=first, b=second)
    await execute(admin_engine, INSERT_FOLLOW, a=second, b=first)
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(first, second))
    with pytest.raises(IntegrityError):  # второй ждущий запрос в том же направлении
        await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(first, second))
    # Встречный запрос возможен: оба профиля могут быть закрыты.
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(second, first))

    # Закрытые запросы не мешают: история копится, ждущий запрос снова возможен.
    await execute(admin_engine, "UPDATE social.follow_requests SET status = 'declined'")
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(first, second))
    await execute(
        admin_engine,
        "UPDATE social.follow_requests SET status = 'cancelled' WHERE status = 'pending'",
    )
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(first, second))
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(first, second, "approved"))
    rows = await fetch_all(
        admin_engine, "SELECT status FROM social.follow_requests ORDER BY created_at, id"
    )
    assert sorted(row["status"] for row in rows) == [
        "approved",
        "cancelled",
        "declined",
        "declined",
        "pending",
    ]


async def test_deleting_an_account_removes_its_follows_and_requests(
    admin_engine: AsyncEngine,
) -> None:
    one, two, three = await accounts(admin_engine, 3)
    for follower, followee in ((one, two), (three, one), (two, three)):
        await execute(admin_engine, INSERT_FOLLOW, a=follower, b=followee)
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(one, three))
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(three, one, "declined"))
    await execute(admin_engine, INSERT_FOLLOW_REQUEST, **follow_request(two, three))

    await execute(admin_engine, "DELETE FROM identity.users WHERE id = :id", id=one)

    assert await follow_counts(admin_engine) == {"follows": 1, "follow_requests": 1}
    left = await fetch_one(admin_engine, "SELECT follower_id, followee_id FROM social.follows")
    assert (left["follower_id"], left["followee_id"]) == (two, three)
    left = await fetch_one(
        admin_engine, "SELECT follower_id, followee_id FROM social.follow_requests"
    )
    assert (left["follower_id"], left["followee_id"]) == (two, three)


async def test_the_app_role_works_with_follows_and_the_readonly_role_only_reads(
    engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    first, second = await accounts(admin_engine)
    async with engine.begin() as connection:  # роль app: полный DML
        await connection.execute(text(INSERT_FOLLOW), {"a": first, "b": second})
        await connection.execute(text(INSERT_FOLLOW_REQUEST), follow_request(second, first))
        await connection.execute(text("UPDATE social.follow_requests SET status = 'approved'"))
        for table in FOLLOW_COLUMNS:
            await connection.execute(text(f"DELETE FROM social.{table}"))

    async def as_readonly(statement: str) -> None:
        async with admin_engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE readonly"))
            await connection.execute(text(statement))

    for table in FOLLOW_COLUMNS:
        await as_readonly(f"SELECT * FROM social.{table}")
        with pytest.raises(ProgrammingError) as caught:
            await as_readonly(f"DELETE FROM social.{table}")
        assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, table


# ----------------------------------------------------------------------------- путь миграции
def _admin(target: DatabaseUnderTest) -> AsyncEngine:
    return create_async_engine(target.admin_url, poolclass=NullPool)


async def test_downgrade_removes_the_graph_and_upgrade_restores_it(
    base_settings: Settings,
) -> None:
    target = await create_database(base_settings)
    admin = _admin(target)
    try:
        down = await run_alembic(["downgrade", "0005"], target.migrator_url)
        assert down.returncode == 0, down.stdout + down.stderr
        tables = await fetch_all(
            admin,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'social'",
        )
        assert tables == []
        up = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
        restored = await fetch_all(
            admin, "SELECT tablename FROM pg_tables WHERE schemaname = 'social' ORDER BY 1"
        )
        assert [row["tablename"] for row in restored] == [
            "blocks",
            "follow_requests",
            "follows",
            "friend_requests",
            "friendships",
        ]
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


async def test_downgrade_to_0006_drops_only_the_follow_tables(base_settings: Settings) -> None:
    """Откат S8 к S7 не трогает дружбу, заявки и блокировки; подъём возвращает подписки."""
    target = await create_database(base_settings)
    admin = _admin(target)
    try:
        first, second = await accounts(admin)
        await execute(
            admin,
            "INSERT INTO social.friendships (user_low_id, user_high_id) VALUES (:a, :b)",
            a=first,
            b=second,
        )
        await execute(admin, INSERT_FOLLOW, a=first, b=second)
        down = await run_alembic(["downgrade", "0006"], target.migrator_url)
        assert down.returncode == 0, down.stdout + down.stderr
        tables = await fetch_all(
            admin, "SELECT tablename FROM pg_tables WHERE schemaname = 'social' ORDER BY 1"
        )
        assert [row["tablename"] for row in tables] == ["blocks", "friend_requests", "friendships"]
        assert (await fetch_one(admin, "SELECT count(*) AS n FROM social.friendships"))["n"] == 1
        up = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
        assert (await fetch_one(admin, "SELECT count(*) AS n FROM social.follows"))["n"] == 0
        assert (await fetch_one(admin, "SELECT count(*) AS n FROM social.friendships"))["n"] == 1
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


async def test_python_and_postgresql_order_uuids_the_same_way(admin_engine: AsyncEngine) -> None:
    """Дружба хранится парой `low < high`: порядок `ordered_pair` обязан совпасть с порядком БД."""
    pairs = [(uuid.uuid4(), uuid.uuid4()) for _ in range(400)]
    pairs += [(uuid7(), uuid7()) for _ in range(100)]
    pairs += [  # старший бит первого байта разный: знаковая арифметика дала бы обратный порядок
        (uuid.UUID(int=1), uuid.UUID(int=2**127)),
        (uuid.UUID(int=2**127 - 1), uuid.UUID(int=2**127)),
    ]

    rows = await fetch_all(
        admin_engine,
        "SELECT a < b AS less, LEAST(a, b) AS low, GREATEST(a, b) AS high "
        "FROM unnest(CAST(:firsts AS uuid[]), CAST(:seconds AS uuid[])) WITH ORDINALITY "
        "AS t(a, b, n) ORDER BY n",
        firsts=[first for first, _ in pairs],
        seconds=[second for _, second in pairs],
    )

    assert len(rows) == len(pairs)
    for (first, second), row in zip(pairs, rows, strict=True):
        assert row["less"] == (first.int < second.int), (first, second)
        assert (row["low"], row["high"]) == ordered_pair(first, second), (first, second)

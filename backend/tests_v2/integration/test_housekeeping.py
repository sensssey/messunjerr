"""Плановая очистка (S2-09): неподтверждённые аккаунты, токены писем, ключи идемпотентности, воркер `default`."""

import asyncio
import hashlib
import uuid
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.idempotency import purge_expired_keys
from messunjerr.core.jobs import (
    QUEUE_DEFAULT,
    TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
    TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
    InMemoryJobQueue,
)
from messunjerr.identity.commands.housekeeping import purge_spent_tokens, purge_unverified_accounts
from messunjerr.jobs.queue import ArqJobQueue
from messunjerr.jobs.worker import build_worker
from messunjerr.settings import Settings

from .helpers import execute, fetch_all, fetch_one, register, verified_user

WEEK = timedelta(days=7)
PURGED_ACTION = "account.unverified_purged"


async def pending_user(client: httpx.AsyncClient, engine: AsyncEngine, *, age_days: int) -> str:
    """Аккаунт `pending`, чьи данные не менялись `age_days` дней и чей токен подтверждения истёк."""
    response, body = await register(client)
    assert response.status_code == 201, response.text
    row = await fetch_one(engine, "SELECT id FROM identity.users WHERE email = :e", e=body["email"])
    user_id = str(row["id"])
    await execute(
        engine,
        "UPDATE identity.users SET updated_at = now() - make_interval(days => :d) WHERE id = :id",
        d=age_days,
        id=uuid.UUID(user_id),
    )
    await execute(
        engine,
        "UPDATE identity.email_tokens SET expires_at = now() - interval '1 hour' WHERE user_id = :id",
        id=uuid.UUID(user_id),
    )
    return user_id


async def user_ids(engine: AsyncEngine) -> set[str]:
    rows = await fetch_all(engine, "SELECT id FROM identity.users")
    return {str(row["id"]) for row in rows}


# ----------------------------------------------------------------------------- аккаунты
async def test_old_unverified_accounts_are_purged_and_everything_else_stays(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    stale = await pending_user(client, admin_engine, age_days=10)
    recent = await pending_user(client, admin_engine, age_days=3)
    active = await verified_user(client, jobs)
    await execute(
        admin_engine,
        "UPDATE identity.users SET updated_at = now() - interval '30 days' WHERE id = :id",
        id=uuid.UUID(active.user_id),
    )
    # Давний аккаунт, но письмо подтверждения запросили вчера: токен ещё жив, человек может успеть.
    waiting = await pending_user(client, admin_engine, age_days=10)
    await execute(
        admin_engine,
        "UPDATE identity.email_tokens SET expires_at = now() + interval '5 hours' WHERE user_id = :id",
        id=uuid.UUID(waiting),
    )

    removed = await purge_unverified_accounts(sessionmaker, older_than=WEEK)

    assert removed == 1
    assert await user_ids(admin_engine) == {recent, active.user_id, waiting}
    assert stale not in await user_ids(admin_engine)
    # Токены и сессии ушли вместе с аккаунтом (каскад), чужие остались.
    leftovers = await fetch_all(
        admin_engine, "SELECT 1 FROM identity.email_tokens WHERE user_id = :id", id=uuid.UUID(stale)
    )
    assert leftovers == []
    assert await fetch_all(admin_engine, "SELECT 1 FROM identity.email_tokens") != []


async def test_a_purge_leaves_a_trail_without_personal_data(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    stale = await pending_user(client, admin_engine, age_days=10)

    await purge_unverified_accounts(sessionmaker, older_than=WEEK)

    row = await fetch_one(
        admin_engine,
        "SELECT actor_id, target_type, target_id, ip, user_agent, data"
        " FROM platform.audit_log WHERE action = :a",
        a=PURGED_ACTION,
    )
    assert row["actor_id"] is None  # действие системы, а не человека
    assert (row["target_type"], row["target_id"]) == ("user", stale)
    assert (row["ip"], row["user_agent"]) == (None, None)
    assert row["data"] == {"older_than_days": 7}  # ни почты, ни ника


async def test_the_address_and_username_are_free_again_after_the_purge(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    response, body = await register(client)
    assert response.status_code == 201
    await execute(admin_engine, "UPDATE identity.users SET updated_at = now() - interval '9 days'")
    await execute(admin_engine, "UPDATE identity.email_tokens SET expires_at = now()")
    taken = await client.get(
        "/api/v1/auth/username-available", params={"username": body["username"]}
    )
    assert taken.json()["available"] is False

    await purge_unverified_accounts(sessionmaker, older_than=WEEK)

    freed = await client.get(
        "/api/v1/auth/username-available", params={"username": body["username"]}
    )
    again = await client.post("/api/v1/auth/register", json=body)
    assert freed.json()["available"] is True
    assert again.status_code == 201
    assert len(await user_ids(admin_engine)) == 1


async def test_the_purge_goes_in_batches_and_a_repeat_finds_nothing(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    stale = {await pending_user(client, admin_engine, age_days=10) for _ in range(5)}
    survivor = await pending_user(client, admin_engine, age_days=1)

    removed = await purge_unverified_accounts(sessionmaker, older_than=WEEK, batch_size=2)
    repeated = await purge_unverified_accounts(sessionmaker, older_than=WEEK, batch_size=2)

    assert (removed, repeated) == (5, 0)
    assert await user_ids(admin_engine) == {survivor}
    audited = await fetch_all(
        admin_engine, "SELECT target_id FROM platform.audit_log WHERE action = :a", a=PURGED_ACTION
    )
    assert {row["target_id"] for row in audited} == stale


async def test_two_workers_purging_at_once_remove_every_account_exactly_once(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    for _ in range(8):
        await pending_user(client, admin_engine, age_days=10)

    counts = await asyncio.gather(
        purge_unverified_accounts(sessionmaker, older_than=WEEK, batch_size=3),
        purge_unverified_accounts(sessionmaker, older_than=WEEK, batch_size=3),
    )

    assert sum(counts) == 8
    assert await user_ids(admin_engine) == set()
    audited = await fetch_all(
        admin_engine, "SELECT 1 FROM platform.audit_log WHERE action = :a", a=PURGED_ACTION
    )
    assert len(audited) == 8


async def test_a_session_of_a_purged_account_is_gone_with_it(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    """У `pending` сессий в норме нет, но связь с каскадом должна работать, если они появятся."""
    stale = await pending_user(client, admin_engine, age_days=10)
    await execute(
        admin_engine,
        "INSERT INTO identity.sessions (user_id, refresh_hash, expires_at, absolute_expires_at)"
        " VALUES (:id, sha256('x'::bytea), now() + interval '1 day', now() + interval '1 day')",
        id=uuid.UUID(stale),
    )

    await purge_unverified_accounts(sessionmaker, older_than=WEEK)

    assert await fetch_all(admin_engine, "SELECT 1 FROM identity.sessions") == []


# ----------------------------------------------------------------------------- токены и ключи
async def test_spent_tokens_are_purged_after_the_grace_period(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    owner = uuid.UUID(user.user_id)
    # метка: (через сколько истекает, сколько назад использован, должен ли токен уйти)
    cases = {
        "expired_long_ago": (timedelta(days=-8), None, True),
        "expired_inside_the_grace": (timedelta(days=-6), None, False),
        "used_long_ago": (timedelta(hours=1), timedelta(days=8), True),
        "used_inside_the_grace": (timedelta(hours=1), timedelta(days=6), False),
        "live": (timedelta(hours=1), None, False),
    }
    now = utcnow()
    for label, (expires_in, used_ago, _) in cases.items():
        await execute(
            admin_engine,
            "INSERT INTO identity.email_tokens (user_id, purpose, token_hash, expires_at, consumed_at)"
            " VALUES (:u, 'reset_password', sha256(convert_to(:label, 'UTF8')), :expires, :used)",
            u=owner,
            label=label,
            expires=now + expires_in,
            used=None if used_ago is None else now - used_ago,
        )

    removed = await purge_spent_tokens(sessionmaker)

    def digest(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()

    remaining = {
        row["h"]
        for row in await fetch_all(
            admin_engine, "SELECT encode(token_hash, 'hex') AS h FROM identity.email_tokens"
        )
    }
    purged = {label for label, (_, _, gone) in cases.items() if gone}
    kept = set(cases) - purged
    assert removed == len(purged) == 2
    assert {digest(label) for label in kept} <= remaining
    assert not {digest(label) for label in purged} & remaining


async def test_expired_idempotency_keys_are_purged_in_batches(
    sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    owner = uuid.uuid4()
    insert = (
        "INSERT INTO platform.idempotency_keys (user_id, key, request_hash, response_status, expires_at)"
        " VALUES (:u, :k, sha256('x'::bytea), 201, :expires)"
    )
    now = utcnow()
    for number in range(5):
        await execute(
            admin_engine, insert, u=owner, k=f"old-{number}", expires=now - timedelta(days=8)
        )
    await execute(
        admin_engine, insert, u=owner, k="inside-the-grace", expires=now - timedelta(days=6)
    )
    await execute(admin_engine, insert, u=owner, k="live", expires=now + timedelta(hours=1))

    removed = await purge_expired_keys(sessionmaker, batch_size=2)
    repeated = await purge_expired_keys(sessionmaker, batch_size=2)

    assert (removed, repeated) == (5, 0)
    left = await fetch_all(admin_engine, "SELECT key FROM platform.idempotency_keys ORDER BY key")
    assert [row["key"] for row in left] == ["inside-the-grace", "live"]


# ----------------------------------------------------------------------------- воркер default
async def test_the_default_worker_runs_both_cleanup_tasks(
    test_settings: Settings,
    client: httpx.AsyncClient,
    admin_engine: AsyncEngine,
) -> None:
    stale = await pending_user(client, admin_engine, age_days=10)
    keeper = await pending_user(client, admin_engine, age_days=1)
    await execute(
        admin_engine,
        "INSERT INTO platform.idempotency_keys (user_id, key, request_hash, response_status, expires_at)"
        " VALUES (:u, 'old', sha256('x'::bytea), 201, now() - interval '8 days')",
        u=uuid.uuid4(),
    )
    for name, days in (("gone_name", -1), ("live_name", 1)):
        await execute(
            admin_engine,
            "INSERT INTO identity.username_reservations (username, user_id, reserved_until)"
            " VALUES (:n, :u, now() + make_interval(days => :d))",
            n=name,
            u=uuid.UUID(keeper),
            d=days,
        )
    queue = ArqJobQueue(test_settings.redis_url.get_secret_value())
    try:
        assert await queue.enqueue(TASK_CLEANUP_UNVERIFIED_ACCOUNTS, queue=QUEUE_DEFAULT)
        assert await queue.enqueue(TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY, queue=QUEUE_DEFAULT)
        worker = build_worker(QUEUE_DEFAULT, test_settings, burst=True, handle_signals=False)
        await worker.async_run()
        await worker.close()
    finally:
        await queue.close()

    assert stale not in await user_ids(admin_engine)
    assert keeper in await user_ids(admin_engine)
    assert await fetch_all(admin_engine, "SELECT 1 FROM platform.idempotency_keys") == []
    left = await fetch_all(
        admin_engine, "SELECT username::text AS u FROM identity.username_reservations"
    )
    assert [row["u"] for row in left] == [
        "live_name"
    ]  # резерв с вышедшим сроком убран, живой остался

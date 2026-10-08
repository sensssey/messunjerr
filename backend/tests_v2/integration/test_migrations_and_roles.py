"""Миграция 0001, роли и права PostgreSQL: «с нуля до head», `alembic check`, откат и привилегии."""

import asyncio
import os
import subprocess
import sys
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.db import SCHEMAS
from messunjerr.core.dbinit import init_database, plan_from_settings
from messunjerr.core.migrations import database_is_ahead, expected_head
from messunjerr.settings import Settings

from .conftest import BACKEND_DIR, DatabaseUnderTest, create_database, drop_database, run_alembic

PLATFORM_TABLES = {"outbox", "inbox", "idempotency_keys", "audit_log"}
INSUFFICIENT_PRIVILEGE = "42501"


async def scalars(engine: AsyncEngine, sql: str, **params: object) -> list[str]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).scalars().all())


async def execute(engine: AsyncEngine, statement: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(statement))


async def assert_denied(engine: AsyncEngine, statement: str) -> None:
    """Операция отклонена именно из-за прав (SQLSTATE 42501), а не по другой причине."""
    with pytest.raises(ProgrammingError) as caught:
        await execute(engine, statement)
    assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE


# ----------------------------------------------------------------------------- миграция
async def test_project_schemas_and_extensions_exist(admin_engine: AsyncEngine) -> None:
    schemas = set(
        await scalars(admin_engine, "SELECT schema_name FROM information_schema.schemata")
    )
    assert set(SCHEMAS) <= schemas
    extensions = set(await scalars(admin_engine, "SELECT extname FROM pg_extension"))
    assert {"citext", "pg_trgm", "unaccent"} <= extensions


async def test_platform_tables_exist(admin_engine: AsyncEngine) -> None:
    tables = set(
        await scalars(admin_engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'platform'")
    )
    assert tables == PLATFORM_TABLES


async def test_database_is_at_the_expected_head(admin_engine: AsyncEngine) -> None:
    (current,) = await scalars(admin_engine, "SELECT version_num FROM alembic_version")
    assert current == expected_head() == "0005"


async def test_alembic_check_sees_no_drift_between_models_and_migrations(
    database: DatabaseUnderTest,
) -> None:
    result = await run_alembic(["check"], database.migrator_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


async def test_uuidv7_default_comes_from_postgresql_18(admin_engine: AsyncEngine) -> None:
    async with admin_engine.begin() as connection:
        event_id = (
            await connection.execute(
                text(
                    "INSERT INTO platform.outbox (topic, key, event_type, payload) "
                    "VALUES ('t', 'k', 'E', '{}') RETURNING event_id"
                )
            )
        ).scalar_one()
    assert isinstance(event_id, uuid.UUID)
    assert event_id.version == 7


async def test_unpublished_index_is_partial(admin_engine: AsyncEngine) -> None:
    (definition,) = await scalars(
        admin_engine,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'platform' "
        "AND indexname = 'ix_outbox_unpublished'",
    )
    assert "published_at IS NULL" in definition


async def test_downgrade_and_upgrade_round_trip(base_settings: Settings) -> None:
    target = await create_database(base_settings)
    try:
        down = await run_alembic(["downgrade", "base"], target.migrator_url)
        assert down.returncode == 0, down.stdout + down.stderr
        engine = _admin(target)
        try:
            left = set(
                await scalars(
                    engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'platform'"
                )
            )
            assert left == set()
        finally:
            await engine.dispose()
        up = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
    finally:
        await drop_database(base_settings, target.name)


def _admin(target: DatabaseUnderTest) -> AsyncEngine:
    return create_async_engine(target.admin_url, poolclass=NullPool)


# ----------------------------------------------------------------------------- роли и права
async def test_roles_exist_and_database_belongs_to_migrator(admin_engine: AsyncEngine) -> None:
    roles = await scalars(
        admin_engine,
        "SELECT rolname FROM pg_roles WHERE rolname IN ('migrator', 'app', 'readonly') "
        "AND rolcanlogin ORDER BY rolname",
    )
    assert {"app", "migrator"} <= set(roles)
    (owner,) = await scalars(
        admin_engine,
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
    )
    assert owner == "migrator"


async def test_app_role_has_safety_timeouts(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        values = {
            name: (await connection.execute(text(f"SHOW {name}"))).scalar_one()
            for name in ("statement_timeout", "idle_in_transaction_session_timeout", "lock_timeout")
        }
    assert values == {
        "statement_timeout": "15s",
        "idle_in_transaction_session_timeout": "30s",
        "lock_timeout": "5s",
    }


async def test_app_role_can_read_the_migration_version(engine: AsyncEngine) -> None:
    """Проверка готовности (`/health/ready`) читает `alembic_version` от имени приложения."""
    async with engine.connect() as connection:
        current = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert current == expected_head()


async def test_app_role_cannot_change_the_migration_version(engine: AsyncEngine) -> None:
    await assert_denied(engine, "UPDATE alembic_version SET version_num = '9999'")


async def test_app_role_can_append_and_read_audit_log(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO platform.audit_log (action) VALUES ('login')"))
    async with engine.connect() as connection:
        actions = (
            (await connection.execute(text("SELECT action FROM platform.audit_log")))
            .scalars()
            .all()
        )
    assert list(actions) == ["login"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE platform.audit_log SET action = 'forged'",
        "DELETE FROM platform.audit_log",
        "TRUNCATE platform.audit_log",
    ],
)
async def test_app_role_cannot_rewrite_the_audit_log(engine: AsyncEngine, statement: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO platform.audit_log (action) VALUES ('login')"))
    await assert_denied(engine, statement)


async def test_app_role_has_full_dml_on_regular_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO platform.outbox (topic, key, event_type, payload) VALUES ('t','k','E','{}')"
            )
        )
        await connection.execute(text("UPDATE platform.outbox SET attempts = attempts + 1"))
        await connection.execute(text("DELETE FROM platform.outbox"))


async def test_app_role_cannot_create_objects(engine: AsyncEngine) -> None:
    await assert_denied(engine, "CREATE TABLE platform.rogue (id int)")


async def test_readonly_role_reads_but_does_not_write(admin_engine: AsyncEngine) -> None:
    async with admin_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE readonly"))
        count = (
            await connection.execute(text("SELECT count(*) FROM platform.outbox"))
        ).scalar_one()
    assert count == 0
    with pytest.raises(ProgrammingError):
        await insert_outbox_row_as_readonly(admin_engine)


async def insert_outbox_row_as_readonly(admin_engine: AsyncEngine) -> None:
    async with admin_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE readonly"))
        await connection.execute(
            text(
                "INSERT INTO platform.outbox (topic, key, event_type, payload) "
                "VALUES ('t', 'k', 'E', '{}')"
            )
        )


async def test_default_privileges_cover_tables_created_later(
    migrator_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    """Новые таблицы, созданные мигратором, сразу доступны приложению: так работают следующие миграции."""
    async with migrator_engine.begin() as connection:
        await connection.execute(text("CREATE TABLE content.probe (id int PRIMARY KEY)"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO content.probe VALUES (1)"))
            assert (
                await connection.execute(text("SELECT count(*) FROM content.probe"))
            ).scalar_one() == 1
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(text("DROP TABLE content.probe"))


# ----------------------------------------------------------------------------- db-init
async def test_db_init_is_idempotent(base_settings: Settings) -> None:
    name = f"mj_test_init_{uuid.uuid4().hex[:8]}"
    plan = plan_from_settings(base_settings, database=name)
    try:
        await init_database(plan)
        await init_database(plan)  # повторный запуск ничего не ломает
    finally:
        await drop_database(base_settings, name)


def test_plan_needs_admin_and_migrator_urls() -> None:
    # Явные None перекрывают переменные окружения контейнера.
    settings = Settings(  # pyright: ignore[reportCallIssue]
        database_url=SecretStr("postgresql+asyncpg://app:p@h/db"),
        redis_url=SecretStr("redis://h/0"),
        admin_database_url=None,
        migrator_database_url=None,
    )
    with pytest.raises(ValueError, match="ADMIN_DATABASE_URL"):
        plan_from_settings(settings)


@pytest.mark.parametrize("bad", ["Bad-Name", "x; drop database y", "", "a" * 64, "1abc"])
def test_plan_rejects_unsafe_database_names(base_settings: Settings, bad: str) -> None:
    with pytest.raises(ValueError, match="Недопустимое имя"):
        plan_from_settings(base_settings, database=bad)


# ----------------------------------------------------------------------------- БД впереди кода (S4)
async def test_database_is_ahead_only_when_its_revision_is_unknown(
    base_settings: Settings,
) -> None:
    target = await create_database(base_settings)
    admin = _admin(target)
    try:
        assert await database_is_ahead(target.migrator_url) is False  # на head
        await execute(admin, "UPDATE alembic_version SET version_num = '9999_future'")
        assert await database_is_ahead(target.migrator_url) is True  # проведена новым релизом
        await execute(admin, "UPDATE alembic_version SET version_num = '0001'")
        assert await database_is_ahead(target.migrator_url) is False  # известная: догонит upgrade
        await execute(admin, "DROP TABLE alembic_version")
        assert await database_is_ahead(target.migrator_url) is False  # пустая база
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


async def run_migrate_command(target: DatabaseUnderTest) -> subprocess.CompletedProcess[str]:
    """`python -m messunjerr migrate` отдельным процессом, как его запускает одноразовая задача Compose."""
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "messunjerr", "migrate"],
        cwd=BACKEND_DIR,
        env={
            **os.environ,
            "DATABASE_URL": target.app_url,
            "MIGRATOR_DATABASE_URL": target.migrator_url,
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


async def test_migrate_command_skips_a_database_that_is_ahead_of_the_code(
    base_settings: Settings,
) -> None:
    # Откат кода после выкладки с миграцией: `alembic upgrade head` упал бы с «Can't locate revision»,
    # и одноразовая задача `migrate` не давала бы поднять сервисы после `docker compose up`.
    target = await create_database(base_settings, migrate=False)
    admin = _admin(target)
    try:
        first = await run_migrate_command(target)
        assert first.returncode == 0, first.stdout + first.stderr
        assert "последней ревизии" in first.stdout
        (current,) = await scalars(admin, "SELECT version_num FROM alembic_version")
        assert current == expected_head()

        await execute(admin, "UPDATE alembic_version SET version_num = '9999_future'")
        second = await run_migrate_command(target)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "БД новее кода" in second.stdout
        (kept,) = await scalars(admin, "SELECT version_num FROM alembic_version")
        assert kept == "9999_future"  # ничего не тронуто
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)

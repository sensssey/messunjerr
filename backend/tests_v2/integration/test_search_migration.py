"""Миграция 0008 (S8-04): индексы поиска людей, откат, права ролей и база без кириллических триграмм."""

import json
import re
import uuid
from typing import cast

import pytest
from sqlalchemy import Table, TextClause, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.dbinit import init_database, plan_from_settings
from messunjerr.identity.infra.models import UserRow
from messunjerr.profiles.infra.models import ProfileRow
from messunjerr.settings import Settings
from messunjerr.social.queries.search import SEARCH_SETTINGS, search_statement

from .conftest import DatabaseUnderTest, create_database, drop_database, run_alembic, with_database
from .helpers import fetch_all, fetch_one
from .search_helpers import Person, add_people

INDEXES = {
    "ix_users_username_trgm": ("identity", "users"),
    "ix_users_username_prefix": ("identity", "users"),
    "ix_profiles_display_name_trgm": ("profile", "profiles"),
}
INSUFFICIENT_PRIVILEGE = "42501"


async def definitions(engine: AsyncEngine) -> dict[str, str]:
    rows = await fetch_all(
        engine,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname IN ('identity', 'profile')",
    )
    return {row["indexname"]: row["indexdef"] for row in rows}


def _admin(target: DatabaseUnderTest) -> AsyncEngine:
    return create_async_engine(target.admin_url, poolclass=NullPool)


# ----------------------------------------------------------------------------- индексы
async def test_the_search_indexes_exist_with_the_expected_definitions(
    admin_engine: AsyncEngine,
) -> None:
    found = await definitions(admin_engine)

    assert set(INDEXES) <= set(found)
    for name, (schema, table) in INDEXES.items():
        assert f"ON {schema}.{table}" in found[name], name
    assert "USING gin" in found["ix_users_username_trgm"]
    assert "(username)::text" in found["ix_users_username_trgm"]
    assert "gin_trgm_ops" in found["ix_users_username_trgm"]
    assert "USING btree" in found["ix_users_username_prefix"]
    assert "text_pattern_ops" in found["ix_users_username_prefix"]
    assert "USING gin" in found["ix_profiles_display_name_trgm"]
    assert (
        "translate(display_name, 'ёЁ'::text, 'еЕ'::text)" in found["ix_profiles_display_name_trgm"]
    )
    assert "gin_trgm_ops" in found["ix_profiles_display_name_trgm"]


def _shape(expression: str) -> str:
    """Выражение индекса без пробелов, скобок и приведений `::text`: так модель и `pg_indexes` сравнимы."""
    return re.sub(r"[()\s]|::text", "", expression)


async def test_the_models_declare_the_same_indexes_as_the_database_has(
    admin_engine: AsyncEngine,
) -> None:
    """`alembic check` не сравнивает выражения индексов, поэтому расхождение модели с миграцией по буквам
    ловит этот тест: выражение из модели против определения индекса в БД (`pg_indexes`)."""
    declared = {
        str(index.name): index
        for table in (cast(Table, UserRow.__table__), cast(Table, ProfileRow.__table__))
        for index in table.indexes
    }
    assert set(INDEXES) <= set(declared)
    assert declared["ix_users_username_trgm"].dialect_options["postgresql"]["using"] == "gin"
    assert declared["ix_profiles_display_name_trgm"].dialect_options["postgresql"]["using"] == "gin"
    stored = await definitions(admin_engine)

    for name in INDEXES:
        in_model = _shape(cast(TextClause, declared[name].expressions[0]).text)
        in_database = _shape(stored[name].split(" USING ", 1)[1].split(" ", 1)[1])
        assert in_model == in_database, (name, in_model, in_database)


async def test_the_test_database_builds_trigrams_for_cyrillic(admin_engine: AsyncEngine) -> None:
    """Условие, без которого поиск по русским именам молчит (см. миграцию): LC_CTYPE базы не `C`."""
    row = await fetch_one(
        admin_engine,
        "SELECT cardinality(show_trgm('Анна')) AS cyrillic, cardinality(show_trgm('anna')) AS latin, "
        "lower('ЁЛКИН') AS lowered, similarity('иван петров', 'Петров Иван') AS reordered",
    )

    assert row["cyrillic"] == 5
    assert row["latin"] == 5
    assert row["lowered"] == "ёлкин"
    assert row["reordered"] == pytest.approx(1.0)


async def explain(engine: AsyncEngine, query: str, settings: list[str]) -> str:
    statement = search_statement(uuid.uuid4(), query, limit=21, offset=0)
    sql = str(statement.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True}))
    async with engine.begin() as connection:
        for name, value in SEARCH_SETTINGS.items():
            await connection.execute(
                text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
            )
        for setting in settings:
            await connection.exec_driver_sql(setting)
        rows = (await connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + sql)).all()
    return json.dumps(rows[0][0], ensure_ascii=False)


@pytest.mark.parametrize(
    ("query", "indexes"),
    [
        ("иван петров", {"ix_profiles_display_name_trgm"}),
        ("елкин", {"ix_profiles_display_name_trgm"}),
        ("john smith", {"ix_profiles_display_name_trgm", "ix_users_username_trgm"}),
        ("ivan_p", {"ix_users_username_prefix", "ix_users_username_trgm"}),
    ],
)
async def test_every_condition_of_the_search_can_use_its_index(
    admin_engine: AsyncEngine, query: str, indexes: set[str]
) -> None:
    """Применимость индексов, а не выбор планировщика (тот зависит от размера таблицы): с запретом
    последовательного и индексного чтения остаётся только чтение битовых карт по индексам условий. Если
    выражение запроса разошлось с индексом хоть на букву, нужного индекса в плане не будет."""
    plan = await explain(
        admin_engine,
        query,
        ["SET LOCAL enable_seqscan = off", "SET LOCAL enable_indexscan = off"],
    )

    for index in indexes:
        assert index in plan, index


# ----------------------------------------------------------------------------- права
async def test_roles_use_the_search_indexes_but_only_the_owner_changes_them(
    engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    await add_people(admin_engine, [Person("anna_p", "Анна Петрова")])
    search = (
        "SELECT u.username::text FROM identity.users u JOIN profile.profiles p ON p.user_id = u.id "
        "WHERE translate(p.display_name, 'ёЁ', 'еЕ') % 'анна' OR u.username::text LIKE 'anna%'"
    )

    async with engine.begin() as connection:  # роль app читает через индексы
        assert (await connection.execute(text(search))).scalars().all() == ["anna_p"]
    async with admin_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE readonly"))
        assert (await connection.execute(text(search))).scalars().all() == ["anna_p"]
    for statement in (
        "DROP INDEX identity.ix_users_username_trgm",
        "CREATE INDEX ix_rogue ON identity.users ((username::text) text_pattern_ops)",
    ):
        with pytest.raises(ProgrammingError) as caught:
            async with engine.begin() as connection:
                await connection.execute(text(statement))
        assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, statement


# ----------------------------------------------------------------------------- путь миграции
async def test_downgrade_removes_the_indexes_and_upgrade_restores_them(
    base_settings: Settings,
) -> None:
    target = await create_database(base_settings)
    admin = _admin(target)
    try:
        down = await run_alembic(["downgrade", "0007"], target.migrator_url)
        assert down.returncode == 0, down.stdout + down.stderr
        assert not set(INDEXES) & set(await definitions(admin))
        up = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
        assert set(INDEXES) <= set(await definitions(admin))
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


# ----------------------------------------------------------------------------- локаль базы
async def _database_with_locale(base_settings: Settings, options: str) -> DatabaseUnderTest:
    """Пустая база с заданной локалью (шаблон `template0`: у `template1` локаль своя), на версии 0007."""
    assert base_settings.admin_database_url is not None
    assert base_settings.migrator_database_url is not None
    name = f"mj_test_{uuid.uuid4().hex[:10]}"
    admin_url = base_settings.admin_database_url.get_secret_value()
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(
                f'CREATE DATABASE "{name}" OWNER migrator ENCODING UTF8 TEMPLATE template0 {options}'
            )
    finally:
        await engine.dispose()
    try:
        await init_database(plan_from_settings(base_settings, database=name))  # роли и права
        target = DatabaseUnderTest(
            name=name,
            app_url=with_database(base_settings.database_url.get_secret_value(), name),
            migrator_url=with_database(
                base_settings.migrator_database_url.get_secret_value(), name
            ),
            admin_url=with_database(admin_url, name),
        )
        before = await run_alembic(["upgrade", "0007"], target.migrator_url)
        assert before.returncode == 0, before.stdout + before.stderr
    except BaseException:
        await drop_database(base_settings, name)  # упавшая подготовка не оставляет базу `mj_test_*`
        raise
    return target


async def test_a_c_locale_database_refuses_the_migration_instead_of_searching_in_silence(
    base_settings: Settings,
) -> None:
    """В базе с `LC_CTYPE = C` кириллица не даёт триграмм, поиск по русским именам не нашёл бы никого."""
    target = await _database_with_locale(
        base_settings, "LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C'"
    )
    admin = _admin(target)
    try:
        broken = await fetch_one(admin, "SELECT cardinality(show_trgm('Анна')) AS trigrams")
        assert broken["trigrams"] == 0  # так выглядит беда

        refused = await run_alembic(["upgrade", "head"], target.migrator_url)

        assert refused.returncode != 0
        output = refused.stdout + refused.stderr
        assert "LC_CTYPE" in output
        assert "C.UTF-8" in output  # подсказка, чем пересоздать базу
        assert not set(INDEXES) & set(await definitions(admin))  # ничего не создано
        version = await fetch_one(admin, "SELECT version_num FROM alembic_version")
        assert version["version_num"] == "0007"
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


@pytest.mark.parametrize(
    "options",
    [
        "LOCALE_PROVIDER builtin LOCALE 'C.UTF-8'",
        "LOCALE_PROVIDER libc LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'",
    ],
    ids=["builtin-c-utf8", "libc-c-utf8"],
)
async def test_the_recommended_locales_pass_the_migration_and_search_cyrillic(
    base_settings: Settings, options: str
) -> None:
    """Решение, которое называет сообщение миграции, проверено: `C.UTF-8` даёт триграммы кириллицы."""
    target = await _database_with_locale(base_settings, options)
    admin = _admin(target)
    try:
        applied = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        assert set(INDEXES) <= set(await definitions(admin))
        row = await fetch_one(
            admin,
            "SELECT cardinality(show_trgm('Анна')) AS trigrams, lower('ЁЛКИН') AS lowered, "
            "'елкин' % translate('Пётр Ёлкин', 'ёЁ', 'еЕ') AS same_letter",
        )
        assert row["trigrams"] == 5
        assert row["lowered"] == "ёлкин"
        assert row["same_letter"] is True
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)

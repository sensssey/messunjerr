"""Первичная настройка PostgreSQL: роли, база, права. Идемпотентна, запускается от администратора.

Роли из 4.16: `migrator` (владелец базы и схем, DDL), `app` (DML, таймауты), `readonly`.
Схемы, расширения и права на таблицы создаёт миграция 0001 уже от имени `migrator`.
"""

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.settings import Settings

ROLE_MIGRATOR = "migrator"
ROLE_APP = "app"
ROLE_READONLY = "readonly"

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Таймауты роли приложения (4.16): запрос не живёт дольше 15 с, транзакция не висит без дела.
_APP_ROLE_SETTINGS = {
    "statement_timeout": "15s",
    "idle_in_transaction_session_timeout": "30s",
    "lock_timeout": "5s",
}


@dataclass(frozen=True, slots=True)
class DbInitPlan:
    admin_url: str
    database: str
    migrator_password: str
    app_password: str
    readonly_password: str | None


def plan_from_settings(settings: Settings, *, database: str | None = None) -> DbInitPlan:
    """Собирает план из настроек. Имя базы берётся из `DATABASE_URL`, если не передано явно."""
    if settings.admin_database_url is None or settings.migrator_database_url is None:
        raise ValueError("Для db-init нужны ADMIN_DATABASE_URL и MIGRATOR_DATABASE_URL")
    app_url = make_url(settings.database_url.get_secret_value())
    migrator_url = make_url(settings.migrator_database_url.get_secret_value())
    name = app_url.database if database is None else database
    if not name or not _IDENTIFIER.match(name):
        raise ValueError(f"Недопустимое имя базы данных: {name!r}")
    if app_url.password is None or migrator_url.password is None:
        raise ValueError("В DATABASE_URL и MIGRATOR_DATABASE_URL должны быть пароли")
    readonly = settings.db_readonly_password
    return DbInitPlan(
        admin_url=settings.admin_database_url.get_secret_value(),
        database=name,
        migrator_password=migrator_url.password,
        app_password=app_url.password,
        readonly_password=readonly.get_secret_value() if readonly else None,
    )


async def _quote_literal(connection: AsyncConnection, value: str) -> str:
    quoted = await connection.scalar(text("SELECT quote_literal(:value)"), {"value": value})
    if not isinstance(quoted, str):
        raise RuntimeError("quote_literal вернул не строку")
    return quoted


async def _ensure_role(connection: AsyncConnection, role: str, password: str | None) -> None:
    exists = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    if password is None:
        # Роль без пароля нужна только как получатель прав; войти под ней нельзя.
        if not exists:
            await connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        return
    literal = await _quote_literal(connection, password)
    verb = "ALTER" if exists else "CREATE"
    await connection.exec_driver_sql(f"{verb} ROLE {role} WITH LOGIN PASSWORD {literal}")


async def _ensure_database(connection: AsyncConnection, database: str) -> None:
    exists = await connection.scalar(
        text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
    )
    if not exists:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER {ROLE_MIGRATOR}')
    await connection.exec_driver_sql(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC')
    await connection.exec_driver_sql(
        f'GRANT CONNECT ON DATABASE "{database}" TO {ROLE_APP}, {ROLE_READONLY}'
    )


async def init_database(plan: DbInitPlan) -> None:
    """Создаёт роли и базу, если их нет; обновляет пароли и таймауты. Безопасно запускать повторно."""
    engine = create_async_engine(plan.admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await _ensure_role(connection, ROLE_MIGRATOR, plan.migrator_password)
            await _ensure_role(connection, ROLE_APP, plan.app_password)
            await _ensure_role(connection, ROLE_READONLY, plan.readonly_password)
            for name, value in _APP_ROLE_SETTINGS.items():
                await connection.exec_driver_sql(f"ALTER ROLE {ROLE_APP} SET {name} = '{value}'")
            await _ensure_database(connection, plan.database)
    finally:
        await engine.dispose()

"""Окружение Alembic: асинхронный запуск от роли `migrator` (адрес из MIGRATOR_DATABASE_URL)."""

import asyncio
import os
from importlib import import_module
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.db import SCHEMAS, Base

# Модули с моделями регистрируют таблицы в Base.metadata; каждый новый контекст добавляется сюда.
for module in (
    "messunjerr.core.models",
    "messunjerr.identity.infra.models",
    "messunjerr.profiles.infra.models",
):
    import_module(module)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    url = os.environ.get("MIGRATOR_DATABASE_URL")
    secret_file = os.environ.get("MIGRATOR_DATABASE_URL_FILE")
    if not url and secret_file:
        url = Path(secret_file).read_text(encoding="utf-8").strip()
    if not url:
        raise RuntimeError("Не задан MIGRATOR_DATABASE_URL (или MIGRATOR_DATABASE_URL_FILE)")
    return url


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Следим только за схемами проекта: служебные схемы и расширения в сравнение не попадают."""
    if type_ == "schema":
        return name is None or name in SCHEMAS
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "Офлайн-режим не поддерживается: миграции содержат управление ролями и правами"
    )
asyncio.run(run_async_migrations())

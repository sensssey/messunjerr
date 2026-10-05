"""Подключение к PostgreSQL: асинхронный движок SQLAlchemy 2.1 (asyncpg) и базовый класс моделей."""

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from messunjerr.settings import Settings

# Единые имена ограничений: их же использует Alembic, поэтому `alembic check` не видит расхождений.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Схемы по контекстам (4.5). Миграция 0001 создаёт их все.
SCHEMAS: tuple[str, ...] = (
    "identity",
    "profile",
    "social",
    "content",
    "chat",
    "notify",
    "media",
    "moderation",
    "platform",
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    """Движок роли `app`. Соединения устанавливаются лениво, поэтому старт не зависит от БД."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            "server_settings": {"application_name": f"messunjerr-{settings.app_env}"},
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)

"""Фикстуры интеграционных тестов: настоящие PostgreSQL 18 и Redis.

Сервисы приходят из окружения: их поднимает Compose (`make test`) или CI. Нужны переменные
ADMIN_DATABASE_URL, DATABASE_URL, MIGRATOR_DATABASE_URL и REDIS_URL; если их нет, тесты
пропускаются с понятным сообщением. Каждый прогон создаёт отдельную базу `mj_test_<hex>`,
доводит её миграциями до head и в конце удаляет, а Redis использует базу №15.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from messunjerr.core.db import SCHEMAS, create_engine, create_sessionmaker
from messunjerr.core.dbinit import init_database, plan_from_settings
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.redis import create_redis
from messunjerr.main import create_app
from messunjerr.settings import Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
REQUIRED_ENV = ("ADMIN_DATABASE_URL", "DATABASE_URL", "MIGRATOR_DATABASE_URL", "REDIS_URL")
REDIS_TEST_DB = 15
TEST_JWT_SEED = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"  # base64url от байтов 0..31


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


def with_database(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


def with_redis_db(url: str, number: int) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{number}"))


async def run_alembic(args: list[str], migrator_url: str) -> subprocess.CompletedProcess[str]:
    """Alembic запускается отдельным процессом: его env.py сам крутит цикл событий."""
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_DIR / "alembic.ini"), *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "MIGRATOR_DATABASE_URL": migrator_url},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@dataclass(frozen=True, slots=True)
class DatabaseUnderTest:
    name: str
    app_url: str
    migrator_url: str
    admin_url: str


async def create_database(settings: Settings, *, migrate: bool = True) -> DatabaseUnderTest:
    name = f"mj_test_{uuid.uuid4().hex[:10]}"
    await init_database(plan_from_settings(settings, database=name))
    assert settings.admin_database_url is not None
    assert settings.migrator_database_url is not None
    target = DatabaseUnderTest(
        name=name,
        app_url=with_database(settings.database_url.get_secret_value(), name),
        migrator_url=with_database(settings.migrator_database_url.get_secret_value(), name),
        admin_url=with_database(settings.admin_database_url.get_secret_value(), name),
    )
    if migrate:
        result = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert result.returncode == 0, result.stdout + result.stderr
    return target


async def drop_database(settings: Settings, name: str) -> None:
    assert settings.admin_database_url is not None
    engine = create_async_engine(
        settings.admin_database_url.get_secret_value(),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        pytest.skip(
            f"нужны переменные {missing}: запускайте тесты через `make test`, сервисы поднимет Compose"
        )
    return Settings()  # pyright: ignore[reportCallIssue]


@pytest_asyncio.fixture(scope="session")
async def database(base_settings: Settings) -> AsyncIterator[DatabaseUnderTest]:
    target = await create_database(base_settings)
    try:
        yield target
    finally:
        await drop_database(base_settings, target.name)


@pytest.fixture(scope="session")
def test_settings(base_settings: Settings, database: DatabaseUnderTest) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        app_env="test",
        log_level="WARNING",
        database_url=SecretStr(database.app_url),
        migrator_database_url=SecretStr(database.migrator_url),
        admin_database_url=SecretStr(database.admin_url),
        db_readonly_password=base_settings.db_readonly_password,
        redis_url=SecretStr(
            with_redis_db(base_settings.redis_url.get_secret_value(), REDIS_TEST_DB)
        ),
        # Постоянный ключ: токены можно разбирать и подделывать в тестах предсказуемо.
        jwt_private_key=SecretStr(TEST_JWT_SEED),
        public_base_url="http://localhost:3000",
        # Argon2id в тестах дешёвый: проверяется логика, а не стойкость параметров.
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_hash_concurrency=2,
        # Адрес SMTP (Mailpit) нужен только сквозному тесту писем; без него тест пропускается.
        smtp_url=base_settings.smtp_url,
        mail_from="messunjerr <no-reply@messunjerr.local>",
    )


@pytest_asyncio.fixture(scope="session")
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Движок роли `app`: именно с её правами работает приложение."""
    created = create_engine(test_settings)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(scope="session")
async def migrator_engine(database: DatabaseUnderTest) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(database.migrator_url, poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(scope="session")
async def admin_engine(database: DatabaseUnderTest) -> AsyncIterator[AsyncEngine]:
    """Суперпользователь: для проверок и очистки между тестами."""
    created = create_async_engine(database.admin_url, poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest_asyncio.fixture(scope="session")
async def redis_client(test_settings: Settings) -> AsyncIterator[Redis]:
    created = create_redis(test_settings)
    try:
        yield created
    finally:
        await created.aclose()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(admin_engine: AsyncEngine, redis_client: Redis) -> None:
    """Каждый тест начинает с пустых таблиц проекта и пустого Redis."""
    async with admin_engine.begin() as connection:
        names = (
            (
                await connection.execute(
                    text(
                        "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                        "WHERE schemaname = ANY(:schemas)"
                    ),
                    {"schemas": list(SCHEMAS)},
                )
            )
            .scalars()
            .all()
        )
        if names:
            await connection.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
    await redis_client.flushdb()  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture
def jobs() -> InMemoryJobQueue:
    """Очередь задач в памяти: тест видит, какие задачи поставила команда (письма и т.п.)."""
    return InMemoryJobQueue()


@pytest_asyncio.fixture
async def app(test_settings: Settings, jobs: InMemoryJobQueue) -> AsyncIterator[FastAPI]:
    application = create_app(test_settings, job_queue=jobs)
    async with LifespanManager(application):
        yield application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

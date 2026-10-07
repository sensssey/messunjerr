"""Проверки готовности (4.15, 5.13): PostgreSQL, Redis и версия миграций.

Kafka, реестр схем и хранилище файлов сюда попадут как `degraded` (они не валят готовность),
когда появятся в плане (S5, S9).
"""

import asyncio
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.migrations import is_known_revision

CHECK_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True)
class Readiness:
    checks: dict[str, str] = field(default_factory=dict[str, str])
    degraded: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def ready(self) -> bool:
        return all(value in ("ok", "head", "ahead") for value in self.checks.values())


async def check_postgres(engine: AsyncEngine) -> str:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return "down"
    return "ok"


async def check_redis(redis: Redis) -> str:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await redis.ping()  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        return "down"
    return "ok"


async def check_migrations(engine: AsyncEngine, expected: str | None) -> str:
    """Сверяет ревизию БД с ревизией кода.

    - `head`: БД на ожидаемой ревизии;
    - `ahead`: ревизии нет среди известных коду, то есть БД проведена более новым релизом. Так
      выглядят старые реплики сразу после миграции при выкладке и предыдущий код при откате.
      Готовность это не нарушает: миграции обязаны быть совместимы в обе стороны («расширить →
      мигрировать → сузить», 4.16), иначе выкладка без простоя невозможна (S4);
    - `behind`: ревизия известна коду, но она не последняя (нужные коду миграции не применены);
    - `unknown`: ответить не удалось (нет миграций рядом с кодом или БД недоступна).
    """
    if expected is None:
        return "unknown"
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()
    except Exception:
        return "unknown"
    if current == expected:
        return "head"
    if current is not None and not is_known_revision(current):
        return "ahead"
    return "behind"


async def run_readiness(engine: AsyncEngine, redis: Redis, expected_head: str | None) -> Readiness:
    postgres, redis_state = await asyncio.gather(check_postgres(engine), check_redis(redis))
    report = Readiness(checks={"postgres": postgres, "redis": redis_state})
    if postgres == "ok":
        report.checks["migrations"] = await check_migrations(engine, expected_head)
    else:
        report.checks["migrations"] = "unknown"
    return report

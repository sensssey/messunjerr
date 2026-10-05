"""Реализация порта `JobQueue` на arq (4.12).

arq по умолчанию сериализует задачи через pickle: тот, кто может писать в Redis, получил бы
выполнение кода в воркере. Поэтому и очередь, и воркер работают с JSON (`json_serializer`).
"""

import json
from datetime import timedelta
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.exceptions import RedisError

from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError
from messunjerr.core.jobs import QUEUE_DEFAULT
from messunjerr.jobs.health import queue_key


def json_serializer(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_deserializer(raw: bytes) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


def redis_settings(redis_url: str) -> RedisSettings:
    return RedisSettings.from_dsn(redis_url)


class ArqJobQueue:
    """Ставит задачи в Redis. Соединение с Redis создаётся лениво при первой постановке."""

    def __init__(self, redis_url: str) -> None:
        self._settings = redis_settings(redis_url)
        self._pool: ArqRedis | None = None

    async def _connection(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                self._settings,
                job_serializer=json_serializer,
                job_deserializer=json_deserializer,
            )
        return self._pool

    async def enqueue(
        self,
        name: str,
        *,
        queue: str = QUEUE_DEFAULT,
        job_id: str | None = None,
        defer_by: timedelta | None = None,
        **kwargs: Any,
    ) -> bool:
        try:
            pool = await self._connection()
            job = await pool.enqueue_job(
                name,
                _queue_name=queue_key(queue),
                _job_id=job_id,
                _defer_by=defer_by,
                **kwargs,
            )
        except (RedisError, OSError, TimeoutError) as error:
            # Откат транзакции команды и честный ответ клиенту: сервис временно недоступен.
            raise DomainError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "The background job queue is unavailable.",
                headers={"Retry-After": "5"},
            ) from error
        return job is not None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

"""Проверка живости воркера для HEALTHCHECK контейнера.

Модуль намеренно лёгкий: только redis-py, без arq, SQLAlchemy и приложения. Docker запускает проверку
каждые несколько секунд, и импорт всего воркера на медленном диске (bind-mount с Windows) не
укладывался в её таймаут.
"""

from redis.asyncio import Redis

QUEUE_KEY_PREFIX = "arq:queue:"


def queue_key(queue: str) -> str:
    """Имя очереди в Redis: все ключи arq лежат под префиксом `arq:` (4.13)."""
    return f"{QUEUE_KEY_PREFIX}{queue}"


async def worker_is_alive(queue: str, redis_url: str) -> bool:
    """Воркер периодически обновляет ключ состояния в Redis; пока ключ есть, он жив."""
    client: Redis = Redis.from_url(redis_url)  # pyright: ignore[reportUnknownMemberType]
    try:
        return bool(
            await client.exists(f"{queue_key(queue)}:health-check")  # pyright: ignore[reportUnknownMemberType]
        )
    except Exception:
        return False
    finally:
        await client.aclose()

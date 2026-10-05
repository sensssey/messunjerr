"""Клиент Redis. В Redis живёт только эфемерное и очередь arq (4.13); источник истины PostgreSQL."""

from redis.asyncio import BlockingConnectionPool, Redis

from messunjerr.settings import Settings


def create_redis(settings: Settings) -> Redis:
    """Клиент с пулом, который при нехватке соединений ждёт, а не падает.

    Обычный пул redis-py при исчерпании сразу бросает `MaxConnectionsError`, а лимитер и замки
    приняли бы это за «Redis недоступен». Короткий всплеск запросов должен выстоять очередь за
    соединением, и только ожидание дольше `redis_pool_timeout_seconds` считается сбоем.
    """
    pool = BlockingConnectionPool.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url.get_secret_value(),
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout_seconds,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )
    return Redis.from_pool(pool)  # `aclose()` закрывает и пул

"""Клиент Redis. В Redis живёт только эфемерное и очередь arq (4.13); источник истины PostgreSQL."""

from redis.asyncio import Redis

from messunjerr.settings import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )

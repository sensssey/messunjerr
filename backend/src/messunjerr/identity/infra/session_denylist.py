"""Denylist отозванных сессий в Redis (4.7, 4.13): ключ `sess:revoked:{sid}` живёт столько же, сколько
access-токен. Источник истины об отзыве это таблица `identity.sessions`; Redis нужен, чтобы уже
выданные access-токены перестали приниматься сразу, а не через 10 минут."""

import uuid
from collections.abc import Iterable

from redis.asyncio import Redis

KEY_PREFIX = "sess:revoked:"


def denylist_key(session_id: uuid.UUID | str) -> str:
    return f"{KEY_PREFIX}{session_id}"


class SessionDenylist:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def revoke(self, session_ids: Iterable[uuid.UUID]) -> None:
        """Добавляет сессии в denylist. Вызывается после коммита; ошибку Redis вызывающий журналирует."""
        ids = list(session_ids)
        if not ids:
            return
        async with self._redis.pipeline(transaction=False) as pipe:  # pyright: ignore[reportUnknownMemberType]
            for session_id in ids:
                pipe.set(denylist_key(session_id), "1", ex=self._ttl)
            await pipe.execute()

    async def is_revoked(self, session_id: uuid.UUID) -> bool:
        """Отозвана ли сессия. Недоступный Redis даёт исключение: решает вызывающий (4.7)."""
        return bool(await self._redis.exists(denylist_key(session_id)))  # pyright: ignore[reportUnknownMemberType]

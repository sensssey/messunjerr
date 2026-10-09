"""Состояние доступа в Redis (4.7, 4.13): denylist отозванных сессий и признак «удаление запрошено».

- `sess:revoked:{sid}` живёт столько же, сколько access-токен. Источник истины об отзыве это таблица
  `identity.sessions`; Redis нужен, чтобы уже выданные access-токены перестали приниматься сразу, а не
  через 20 минут.
- `acct:deletion:{user_id}` стоит, пока аккаунт ждёт удаления (S3-06). Статус из БД на каждый запрос
  не читается (4.7), а текущая сессия после `DELETE /me` остаётся живой, поэтому признак ставит
  команда, а выдача нового токена (вход, refresh) его подтверждает. Срок признака чуть больше срока
  токена: потерянный Redis не оставляет доступ дольше следующего обновления токена, а забытый после
  восстановления признак гаснет сам.

Оба ключа читает одна команда `MGET`: лишнего обращения к Redis на запрос нет.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from redis.asyncio import Redis

KEY_PREFIX = "sess:revoked:"
DELETION_KEY_PREFIX = "acct:deletion:"
DELETION_FLAG_MARGIN_SECONDS = 300


def denylist_key(session_id: uuid.UUID | str) -> str:
    return f"{KEY_PREFIX}{session_id}"


def deletion_key(user_id: uuid.UUID | str) -> str:
    return f"{DELETION_KEY_PREFIX}{user_id}"


@dataclass(frozen=True, slots=True)
class AccessState:
    """Что Redis знает о запросе: отозвана ли сессия и ждёт ли аккаунт удаления."""

    revoked: bool = False
    deletion_pending: bool = False


class SessionDenylist:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._deletion_ttl = ttl_seconds + DELETION_FLAG_MARGIN_SECONDS

    async def revoke(self, session_ids: Iterable[uuid.UUID]) -> None:
        """Добавляет сессии в denylist. Вызывается после коммита; ошибку Redis вызывающий журналирует."""
        ids = list(session_ids)
        if not ids:
            return
        async with self._redis.pipeline(transaction=False) as pipe:  # pyright: ignore[reportUnknownMemberType]
            for session_id in ids:
                pipe.set(denylist_key(session_id), "1", ex=self._ttl)
            await pipe.execute()

    async def access_state(self, session_id: uuid.UUID, user_id: uuid.UUID) -> AccessState:
        """Отзыв сессии и признак удаления одним обращением. Недоступный Redis даёт исключение."""
        values = await self._redis.mget(  # pyright: ignore[reportUnknownMemberType]
            [denylist_key(session_id), deletion_key(user_id)]
        )
        revoked, pending = values
        return AccessState(revoked=revoked is not None, deletion_pending=pending is not None)

    async def mark_deletion_pending(self, user_id: uuid.UUID) -> None:
        await self._redis.set(deletion_key(user_id), "1", ex=self._deletion_ttl)  # pyright: ignore[reportUnknownMemberType]

    async def clear_deletion_pending(self, user_id: uuid.UUID) -> None:
        await self._redis.delete(deletion_key(user_id))  # pyright: ignore[reportUnknownMemberType]

"""Хэширование паролей: Argon2id (pwdlib) в отдельном пуле потоков.

Argon2id намеренно тяжёлый (десятки миллисекунд процессора и десятки мегабайт памяти на хэш).
Вызов в цикле событий остановил бы весь процесс, поэтому работа идёт в `ThreadPoolExecutor`
ограниченного размера: argon2-cffi отпускает GIL, потоки работают параллельно, а число
одновременных хэшей (и, значит, расход памяти) ограничено `PASSWORD_HASH_CONCURRENCY`.
"""

import asyncio
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Self

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    valid: bool
    new_hash: str | None = None
    """Хэш с актуальными параметрами, если параметры изменились: его стоит сохранить."""


class PasswordService:
    def __init__(
        self, *, time_cost: int, memory_cost_kib: int, parallelism: int, concurrency: int
    ) -> None:
        self._hasher = PasswordHash(
            (
                Argon2Hasher(
                    time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
                ),
            )
        )
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="argon2")
        self._decoy_hash: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            time_cost=settings.argon2_time_cost,
            memory_cost_kib=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
            concurrency=settings.password_hash_concurrency,
        )

    async def _in_pool[T](self, function: Callable[..., T], *args: str) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(function, *args))

    async def hash(self, password: str) -> str:
        return await self._in_pool(self._hasher.hash, password)

    async def verify(self, password: str, hashed: str) -> PasswordCheck:
        valid, new_hash = await self._in_pool(self._hasher.verify_and_update, password, hashed)
        return PasswordCheck(valid=valid, new_hash=new_hash if valid else None)

    async def warm_up(self) -> str:
        """Готовит приманку для `burn` (один раз при старте, чтобы первый вход не был медленнее)."""
        if self._decoy_hash is None:
            self._decoy_hash = await self.hash(secrets.token_urlsafe(16))
        return self._decoy_hash

    async def burn(self, password: str) -> None:
        """Тратит столько же времени, сколько проверка настоящего пароля.

        Нужна там, где аккаунта нет: по времени ответа нельзя отличить «нет такого логина» от
        «неверный пароль» (4.14, перечисление пользователей).
        """
        decoy = await self.warm_up()
        await self._in_pool(self._hasher.verify, password, decoy)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

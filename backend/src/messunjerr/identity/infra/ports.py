"""Порты identity к контекстам выше: профили и настройки приватности (S3).

identity стоит ниже `profiles` в графе 4.2 и импортировать его не может. Поэтому регистрации нужно
создать профиль, а входу отдать его в `MeUser`, через эти порты; реализации подставляет корень
приложения (`messunjerr.main`), так же будет с счётчиками друзей, уведомлений и бесед (S7, S10, S14).
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import MeExtras


@dataclass(frozen=True, slots=True)
class ProfileSeed:
    """Что регистрация передаёт профилю; пустые значения означают «по умолчанию» (имя равно нику)."""

    display_name: str | None = None
    language: str | None = None
    timezone: str | None = None


class ProfileProvisioner(Protocol):
    async def provision(
        self, session: AsyncSession, *, user_id: uuid.UUID, username: str, seed: ProfileSeed
    ) -> None:
        """Создаёт профиль и настройки приватности нового аккаунта в транзакции вызывающего.

        Повторная регистрация неподтверждённого адреса заменяет сведения профиля: прежние данные
        принадлежат тому, кто занял чужую почту (4.14, pre-hijacking). Приватность у такого аккаунта
        всегда по умолчанию: без подтверждения почты изменить её нечем.
        """
        ...


class MeExtrasProvider(Protocol):
    async def load(self, session: AsyncSession, user_id: uuid.UUID) -> MeExtras:
        """Профиль, приватность, счётчики и обязательные действия пользователя для `MeUser`."""
        ...

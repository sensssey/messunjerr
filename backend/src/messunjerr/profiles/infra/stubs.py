"""Заглушки портов профилей, пока контексты выше не написаны (S3). Каждую заменит свой спринт."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import MeCounters
from messunjerr.core.uow import UnitOfWork
from messunjerr.profiles.domain.ports import (
    AvatarCheck,
    ProfileCounts,
    RelationshipView,
)


class NoMediaYet:
    """Без контекста media ресурсов нет: любой `avatar_asset_id` это `asset_not_found`.

    Приложение подставляет настоящую реализацию (`messunjerr.media.commands.avatars`); заглушка нужна
    тестам, которые собирают профили отдельно.
    """

    async def check(
        self, session: AsyncSession, *, owner_id: uuid.UUID, asset_id: uuid.UUID
    ) -> AvatarCheck:
        return AvatarCheck.NOT_FOUND

    async def release(self, uow: UnitOfWork, *, owner_id: uuid.UUID, asset_id: uuid.UUID) -> None:
        return None


class NoGraphYet:
    """До S7–S8 друзей, подписок и блокировок нет: зритель либо владелец, либо посторонний."""

    async def between(
        self, session: AsyncSession, *, viewer_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RelationshipView:
        return RelationshipView(is_self=viewer_id == owner_id)


class ZeroCounters:
    """До S7, S8 и S11 считать нечего: все счётчики равны нулю."""

    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> ProfileCounts:
        return ProfileCounts()


class ZeroMeCounters:
    """Счётчики шапки клиента без контекстов выше (заявки в друзья, уведомления, беседы): нули."""

    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> MeCounters:
        return MeCounters()


class NoFollowsYet:
    """Без социального графа подписок нет, и открытие профиля не на ком проверять: ничего не делает.

    Приложение подставляет настоящую реализацию (`messunjerr.social.commands.follows`); заглушка
    нужна тестам и командам, которые собирают профили отдельно (`messunjerr.admin`).
    """

    async def profile_opened(self, uow: UnitOfWork, *, owner_id: uuid.UUID) -> None:
        return None

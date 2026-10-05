"""Заглушки портов профилей, пока контексты выше не написаны (S3). Каждую заменит свой спринт."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.profiles.domain.ports import (
    AvatarCheck,
    ProfileCounts,
    RelationshipView,
)


class NoMediaYet:
    """До S5–S6 ресурсов нет: любой `avatar_asset_id` это `asset_not_found` (план спринтов, S3-02)."""

    async def check(
        self, session: AsyncSession, *, owner_id: uuid.UUID, asset_id: uuid.UUID
    ) -> AvatarCheck:
        return AvatarCheck.NOT_FOUND


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

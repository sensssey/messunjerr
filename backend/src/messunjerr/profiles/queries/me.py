"""Чтение собственного профиля и настроек приватности: разделы `MeUser` и ответы `PATCH` (5.3)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import (
    MeCounters,
    MeExtras,
    MeProfile,
    PrivacySettings,
    ProfileLink,
    avatar_for,
)
from messunjerr.profiles.domain.errors import ProfileMissingError
from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow


def profile_dto(row: ProfileRow) -> MeProfile:
    """Профиль владельца целиком. `hidden_fields` пуст: реестр согласий на распространение в бэклоге (B-01)."""
    return MeProfile(
        display_name=row.display_name,
        avatar=avatar_for(row.avatar_asset_id),
        bio=row.bio,
        links=[ProfileLink(title=link["title"], url=link["url"]) for link in row.links],
        birth_date=row.birth_date,
        birth_date_visibility=row.birth_date_visibility,
        city=row.city,
        language=row.language,
        timezone=row.timezone,
        is_private=row.is_private,
        hidden_fields=[],
    )


def privacy_dto(row: PrivacySettingsRow) -> PrivacySettings:
    return PrivacySettings(
        dm_policy=row.dm_policy,
        comment_policy=row.comment_policy,
        mention_policy=row.mention_policy,
        friends_list_visibility=row.friends_list_visibility,
        followers_list_visibility=row.followers_list_visibility,
        presence_visibility=row.presence_visibility,
        default_post_visibility=row.default_post_visibility,
    )


async def get_privacy(session: AsyncSession, user_id: uuid.UUID) -> PrivacySettings:
    """`GET /me/privacy`: настройки приватности владельца."""
    row = (
        await session.execute(
            select(PrivacySettingsRow).where(PrivacySettingsRow.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ProfileMissingError(user_id)
    return privacy_dto(row)


async def load_me_extras(session: AsyncSession, user_id: uuid.UUID) -> MeExtras:
    """Профиль, приватность, счётчики и обязательные действия для `MeUser` одним запросом.

    Счётчики пока нулевые (друзья, уведомления и беседы появятся в S7, S10, S14), обязательных
    действий в v1 нет (бэклог B-01).
    """
    row = (
        await session.execute(
            select(ProfileRow, PrivacySettingsRow)
            .join(PrivacySettingsRow, PrivacySettingsRow.user_id == ProfileRow.user_id)
            .where(ProfileRow.user_id == user_id)
        )
    ).one_or_none()
    if row is None:
        raise ProfileMissingError(user_id)
    profile, privacy = row
    return MeExtras(
        profile=profile_dto(profile),
        privacy=privacy_dto(privacy),
        counters=MeCounters(),
        required_actions=[],
    )

"""Настройки владельца, от которых зависит чужой доступ к спискам: закрытость профиля и видимость списков."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import ListVisibility
from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow


@dataclass(frozen=True, slots=True)
class OwnerPrivacy:
    """Что о владельце нужно политикам списков (4.6): другие настройки им не интересны."""

    is_private: bool
    friends_list_visibility: ListVisibility
    followers_list_visibility: ListVisibility


async def load_owner_privacy(session: AsyncSession, user_id: uuid.UUID) -> OwnerPrivacy | None:
    """`None`, если профиля нет (аккаунт без профиля это сбой данных, его ловит вызывающий)."""
    row = (
        await session.execute(
            select(
                ProfileRow.is_private,
                PrivacySettingsRow.friends_list_visibility,
                PrivacySettingsRow.followers_list_visibility,
            )
            .join(PrivacySettingsRow, PrivacySettingsRow.user_id == ProfileRow.user_id)
            .where(ProfileRow.user_id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return OwnerPrivacy(
        is_private=row.is_private,
        friends_list_visibility=row.friends_list_visibility,
        followers_list_visibility=row.followers_list_visibility,
    )

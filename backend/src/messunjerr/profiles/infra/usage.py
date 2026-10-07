"""Привязки профилей к ресурсам медиа: аватар (5.8, `asset_in_use`).

Класс реализует порт `AssetUsage` контекста media структурно, ничего из media не импортируя
(profiles стоит ниже media в графе 4.2): корень приложения передаёт его в службы медиа.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.profiles.infra.models import ProfileRow


class ProfileAvatarUsage:
    async def is_attached(self, session: AsyncSession, asset_id: uuid.UUID) -> bool:
        statement = (
            select(ProfileRow.user_id).where(ProfileRow.avatar_asset_id == asset_id).limit(1)
        )
        return (await session.execute(statement)).first() is not None

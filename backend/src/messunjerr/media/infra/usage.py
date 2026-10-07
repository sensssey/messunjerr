"""Сборка «к чему привязан ресурс» из частей: каждый контекст отвечает за свои привязки."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.media.domain.ports import AssetUsage


class CompositeAssetUsage:
    """Ресурс привязан, если привязан хотя бы по одной из частей (аватар, пост, сообщение)."""

    def __init__(self, parts: Sequence[AssetUsage] = ()) -> None:
        self._parts = tuple(parts)

    async def is_attached(self, session: AsyncSession, asset_id: uuid.UUID) -> bool:
        for part in self._parts:
            if await part.is_attached(session, asset_id):
                return True
        return False

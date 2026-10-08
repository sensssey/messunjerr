"""Сборка «к чему привязан ресурс» и «кто его видит» из частей: каждый контекст отвечает за свои привязки."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.media.domain.ports import AssetAudience, AssetUsage


class CompositeAssetUsage:
    """Ресурс привязан, если привязан хотя бы по одной из частей (аватар, пост, сообщение)."""

    def __init__(self, parts: Sequence[AssetUsage] = ()) -> None:
        self._parts = tuple(parts)

    async def is_attached(self, session: AsyncSession, asset_id: uuid.UUID) -> bool:
        for part in self._parts:
            if await part.is_attached(session, asset_id):
                return True
        return False


class CompositeAssetAudience:
    """Зритель видит ресурс, если его видит хотя бы одна часть (видимый пост, беседа, где он участник).

    Частей пока нет (посты S11, сообщения S14): ссылки получает только владелец.
    """

    def __init__(self, parts: Sequence[AssetAudience] = ()) -> None:
        self._parts = tuple(parts)

    async def can_view(
        self, session: AsyncSession, *, asset_id: uuid.UUID, viewer_id: uuid.UUID
    ) -> bool:
        for part in self._parts:
            if await part.can_view(session, asset_id=asset_id, viewer_id=viewer_id):
                return True
        return False

"""Чтение ресурсов для владельца: карточка и квота (5.8)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.infra.repositories import AssetRepository
from messunjerr.media.queries.models import Asset, AssetUrls, Quota
from messunjerr.settings import Settings


def asset_dto(row: AssetRow) -> Asset:
    """Карточка из строки. Ссылки (`urls`) пока всегда пусты: presigned GET появится в S6."""
    return Asset(
        id=row.id,
        purpose=Purpose(row.purpose),
        kind=Kind(row.kind),
        status=Status(row.status),
        filename=row.original_filename,
        content_type=row.content_type,
        declared_size=row.declared_size,
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
        reject_reason=RejectReason(row.reject_reason) if row.reject_reason else None,
        urls=AssetUrls(),
        url_expires_at=None,
        created_at=row.created_at,
        uploaded_at=row.uploaded_at,
        processed_at=row.processed_at,
    )


async def get_asset(
    session: AsyncSession, *, asset_id: uuid.UUID, owner_id: uuid.UUID
) -> Asset | None:
    """Карточка своего ресурса; чужой, несуществующий и удалённый одинаково `None` (4.6)."""
    row = await AssetRepository(session).get(asset_id, owner_id=owner_id)
    return None if row is None else asset_dto(row)


async def get_quota(session: AsyncSession, *, owner_id: uuid.UUID, settings: Settings) -> Quota:
    """Занятое место: размер готовых ресурсов плюс заявленный размер идущих загрузок."""
    usage = await AssetRepository(session).usage(owner_id)
    return Quota(
        used_bytes=usage.used_bytes,
        limit_bytes=settings.media_quota_bytes,
        assets_count=usage.assets_count,
    )

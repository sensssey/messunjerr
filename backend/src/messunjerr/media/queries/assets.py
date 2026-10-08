"""Чтение ресурсов: карточка владельца, ссылки на файлы и квота (5.8)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.infra.repositories import AssetRepository
from messunjerr.media.queries.models import Asset, AssetLinks, AssetUrls, Quota
from messunjerr.settings import Settings

if TYPE_CHECKING:
    from messunjerr.media.domain.ports import AssetAudience
    from messunjerr.media.queries.presenter import AssetPresenter


def asset_dto(
    row: AssetRow, urls: AssetUrls | None = None, url_expires_at: datetime | None = None
) -> Asset:
    """Карточка из строки. Без `urls` ссылки пусты: их выдаёт `AssetPresenter` у готовых ресурсов."""
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
        urls=urls or AssetUrls(),
        url_expires_at=url_expires_at,
        created_at=row.created_at,
        uploaded_at=row.uploaded_at,
        processed_at=row.processed_at,
    )


async def get_asset(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    owner_id: uuid.UUID,
    presenter: "AssetPresenter",
) -> Asset | None:
    """Карточка своего ресурса со ссылками; чужой, несуществующий и удалённый одинаково `None` (4.6)."""
    row = await AssetRepository(session).get(asset_id, owner_id=owner_id)
    return None if row is None else await presenter.card(row)


async def get_asset_links(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    viewer_id: uuid.UUID,
    presenter: "AssetPresenter",
    audience: "AssetAudience",
) -> AssetLinks | None:
    """Свежие ссылки для владельца и для тех, кто вправе видеть объект, к которому ресурс привязан.

    Остальным (чужой ресурс, несуществующий, удалённый) `None`: наружу это `404`, чтобы по ответу
    нельзя было перебирать чужие идентификаторы.
    """
    row = await AssetRepository(session).get(asset_id)
    if row is None:
        return None
    if row.owner_id != viewer_id and not await audience.can_view(
        session, asset_id=row.id, viewer_id=viewer_id
    ):
        return None
    links = await presenter.links(row)
    return AssetLinks(urls=links.urls, url_expires_at=links.expires_at)


async def get_quota(session: AsyncSession, *, owner_id: uuid.UUID, settings: Settings) -> Quota:
    """Занятое место: размер готовых ресурсов плюс заявленный размер идущих загрузок."""
    usage = await AssetRepository(session).usage(owner_id)
    return Quota(
        used_bytes=usage.used_bytes,
        limit_bytes=settings.media_quota_bytes,
        assets_count=usage.assets_count,
    )

"""Список блокировок `GET /me/blocks` (5.4): кого человек заблокировал, новые сверху."""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.pagination import Cursor, Page, decode_cursor, encode_cursor, split_page
from messunjerr.social.infra.models import BlockRow
from messunjerr.social.queries.cards import CARD_COLUMNS, summary_of, with_cards
from messunjerr.social.queries.models import BlockEntry


class BlockCursor(Cursor):
    """Ключ сортировки списка блокировок: время блокировки и заблокированный."""

    blocked_at: datetime
    id: uuid.UUID


async def list_blocks(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int, cursor: str | None
) -> Page[BlockEntry]:
    """Свои блокировки. Фильтра по статусу нет: человек видит, кого заблокировал, даже если тот
    ушёл (скрытие аккаунтов касается чужих глаз, а не собственного списка)."""
    statement = with_cards(
        select(BlockRow.created_at, *CARD_COLUMNS).where(BlockRow.blocker_id == user_id),
        BlockRow.blocked_id,
        only_active=False,
    )
    if cursor is not None:
        position = decode_cursor(cursor, BlockCursor)
        statement = statement.where(
            tuple_(BlockRow.created_at, BlockRow.blocked_id)
            < tuple_(position.blocked_at, position.id)
        )
    rows = (
        await session.execute(
            statement.order_by(BlockRow.created_at.desc(), BlockRow.blocked_id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    page, has_more = split_page(rows, limit)
    next_cursor = (
        encode_cursor(BlockCursor(blocked_at=page[-1].created_at, id=page[-1].card_id))
        if has_more
        else None
    )
    return Page(
        items=[BlockEntry(user=summary_of(row), blocked_at=row.created_at) for row in page],
        next_cursor=next_cursor,
    )

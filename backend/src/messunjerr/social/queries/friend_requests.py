"""Список заявок в друзья `GET /friend-requests` (5.4) и карточка одной заявки для ответов команд."""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.pagination import (
    Cursor,
    Page,
    decode_cursor,
    encode_cursor,
    split_page,
)
from messunjerr.social.domain.rules import Direction, FriendRequestStatus
from messunjerr.social.infra.models import FriendRequestRow, is_pending
from messunjerr.social.queries.cards import CARD_COLUMNS, load_summaries, summary_of, with_cards
from messunjerr.social.queries.models import FriendRequest


class RequestCursor(Cursor):
    """Ключ сортировки списка заявок: время создания и идентификатор (новые сверху)."""

    created_at: datetime
    id: uuid.UUID


async def list_friend_requests(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    direction: Direction,
    limit: int,
    cursor: str | None,
) -> Page[FriendRequest]:
    """Активные заявки: `incoming` пришли человеку, `outgoing` отправил он. Собеседник это сторона
    напротив; аккаунты не `active` в списке не показываются."""
    own, other = (
        (FriendRequestRow.receiver_id, FriendRequestRow.sender_id)
        if direction is Direction.INCOMING
        else (FriendRequestRow.sender_id, FriendRequestRow.receiver_id)
    )
    statement = with_cards(
        select(FriendRequestRow.id, FriendRequestRow.created_at, *CARD_COLUMNS).where(
            own == user_id, is_pending()
        ),
        other,
    )
    if cursor is not None:
        position = decode_cursor(cursor, RequestCursor)
        statement = statement.where(
            tuple_(FriendRequestRow.created_at, FriendRequestRow.id)
            < tuple_(position.created_at, position.id)
        )
    rows = (
        await session.execute(
            statement.order_by(
                FriendRequestRow.created_at.desc(), FriendRequestRow.id.desc()
            ).limit(limit + 1)
        )
    ).all()
    page, has_more = split_page(rows, limit)
    items = [
        FriendRequest(
            id=row.id,
            status="pending",
            user=summary_of(row),
            direction=direction.value,
            created_at=row.created_at,
        )
        for row in page
    ]
    next_cursor = (
        encode_cursor(RequestCursor(created_at=page[-1].created_at, id=page[-1].id))
        if has_more
        else None
    )
    return Page(items=items, next_cursor=next_cursor)


async def friend_request_card(
    session: AsyncSession, row: FriendRequestRow, *, viewer_id: uuid.UUID
) -> FriendRequest:
    """Заявка глазами `viewer_id` (сторона напротив и направление) для ответов `POST`."""
    outgoing = row.sender_id == viewer_id
    counterpart = row.receiver_id if outgoing else row.sender_id
    summaries = await load_summaries(session, [counterpart])
    return FriendRequest(
        id=row.id,
        status="accepted" if row.status == FriendRequestStatus.ACCEPTED else "pending",
        user=summaries[counterpart],
        direction=Direction.OUTGOING.value if outgoing else Direction.INCOMING.value,
        created_at=row.created_at,
    )

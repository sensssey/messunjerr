"""Списки друзей (5.4, 5.3): свои, чужие с отношением к зрителю и общие.

Порядок везде по давности дружбы, новые сверху; страницы по ключу `(дата, человек)`. Человек не виден
в списке, если его аккаунт не `active` или между ним и зрителем есть блокировка в любую сторону
(4.6: такие люди для зрителя не существуют).
"""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, case, exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Subquery

from messunjerr.core.pagination import Cursor, Page, decode_cursor, encode_cursor, split_page
from messunjerr.profiles.api_public import UserSummary
from messunjerr.social.infra.directory import profiles, users
from messunjerr.social.infra.models import BlockRow, FriendshipRow
from messunjerr.social.queries.cards import CARD_COLUMNS, Expr, summary_of, with_cards
from messunjerr.social.queries.models import FriendEntry, UserListItem
from messunjerr.social.queries.relationships import relationships_for


class FriendCursor(Cursor):
    """Ключ сортировки списков друзей: дата дружбы и идентификатор друга."""

    since: datetime
    id: uuid.UUID


def escape_like(text: str) -> str:
    """Экранирует `%`, `_` и обратную косую черту: поисковая строка человека не шаблон."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _friends_of(user_id: uuid.UUID, name: str) -> Subquery:
    """Друзья человека одним столбцом `friend_id` и датой дружбы `since`."""
    friend_of = case(
        (FriendshipRow.user_low_id == user_id, FriendshipRow.user_high_id),
        else_=FriendshipRow.user_low_id,
    ).label("friend_id")
    return (
        select(friend_of, FriendshipRow.created_at.label("since"))
        .where(or_(FriendshipRow.user_low_id == user_id, FriendshipRow.user_high_id == user_id))
        .subquery(name)
    )


def _hide_blocked[*Ts](statement: Select[*Ts], viewer_id: uuid.UUID, person: Expr) -> Select[*Ts]:
    """Убирает людей, с которыми у зрителя блокировка в любую сторону."""
    return statement.where(
        ~exists().where(
            or_(
                and_(BlockRow.blocker_id == viewer_id, BlockRow.blocked_id == person),
                and_(BlockRow.blocker_id == person, BlockRow.blocked_id == viewer_id),
            )
        )
    )


async def _page_of_friends[*Ts](
    session: AsyncSession,
    statement: Select[*Ts],
    since_column: Expr,
    friend_column: Expr,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """Страница по ключу `(since, friend_id)` по убыванию; курсор следующей страницы или `None`."""
    if cursor is not None:
        position = decode_cursor(cursor, FriendCursor)
        statement = statement.where(
            tuple_(since_column, friend_column) < tuple_(position.since, position.id)
        )
    rows = (
        await session.execute(
            statement.order_by(since_column.desc(), friend_column.desc()).limit(limit + 1)
        )
    ).all()
    page, has_more = split_page(rows, limit)
    next_cursor = (
        encode_cursor(FriendCursor(since=page[-1].since, id=page[-1].card_id)) if has_more else None
    )
    return page, next_cursor


async def list_my_friends(
    session: AsyncSession, *, user_id: uuid.UUID, q: str | None, limit: int, cursor: str | None
) -> Page[FriendEntry]:
    """`GET /friends`: друзья человека, при `q` только те, чей ник или имя содержат строку."""
    friends = _friends_of(user_id, "friends")
    statement = with_cards(
        select(friends.c.since, *CARD_COLUMNS).select_from(friends), friends.c.friend_id
    )
    if q:
        pattern = f"%{escape_like(q)}%"
        statement = statement.where(
            or_(
                users.c.username.ilike(pattern, escape="\\"),
                profiles.c.display_name.ilike(pattern, escape="\\"),
            )
        )
    statement = _hide_blocked(statement, user_id, friends.c.friend_id)
    rows, next_cursor = await _page_of_friends(
        session, statement, friends.c.since, friends.c.friend_id, limit=limit, cursor=cursor
    )
    return Page(
        items=[FriendEntry(user=summary_of(row), since=row.since) for row in rows],
        next_cursor=next_cursor,
    )


async def list_user_friends(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    viewer_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> Page[UserListItem]:
    """`GET /users/{ref}/friends`: друзья владельца с отношением зрителя к каждому. Доступ к списку
    решает политика до вызова."""
    friends = _friends_of(owner_id, "friends")
    statement = with_cards(
        select(friends.c.since, *CARD_COLUMNS).select_from(friends), friends.c.friend_id
    )
    statement = _hide_blocked(statement, viewer_id, friends.c.friend_id)
    rows, next_cursor = await _page_of_friends(
        session, statement, friends.c.since, friends.c.friend_id, limit=limit, cursor=cursor
    )
    relationships = await relationships_for(session, viewer_id, [row.card_id for row in rows])
    return Page(
        items=[
            UserListItem(**summary_of(row).model_dump(), relationship=relationships[row.card_id])
            for row in rows
        ],
        next_cursor=next_cursor,
    )


async def list_mutual_friends(
    session: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    owner_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> Page[UserSummary]:
    """`GET /users/{ref}/mutual-friends`: люди, которые друзья и зрителю, и владельцу.

    У собственного профиля общих друзей нет (человек сам себе не «общий»): страница пустая.
    """
    if viewer_id == owner_id:
        return Page(items=[], next_cursor=None)
    mine = _friends_of(viewer_id, "mine")
    theirs = _friends_of(owner_id, "theirs")
    statement = with_cards(
        select(mine.c.since, *CARD_COLUMNS)
        .select_from(mine)
        .join(theirs, theirs.c.friend_id == mine.c.friend_id),
        mine.c.friend_id,
    )
    rows, next_cursor = await _page_of_friends(
        session, statement, mine.c.since, mine.c.friend_id, limit=limit, cursor=cursor
    )
    return Page(items=[summary_of(row) for row in rows], next_cursor=next_cursor)

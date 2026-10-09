"""Подписчики, подписки и запросы на подписку (5.4, 5.3): свои списки, чужие и входящие запросы.

Порядок везде по давности, новые сверху; страницы по ключу `(дата, человек)`. Человек не виден в
списке, если его аккаунт не `active` или между ним и зрителем есть блокировка в любую сторону (4.6:
такие люди для зрителя не существуют). Доступ к чужому списку решает политика `decide_list_access`
с настройкой владельца `followers_list_visibility`: она управляет и подписчиками, и подписками.
"""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.errors import NotFoundError
from messunjerr.core.pagination import Cursor, Page, decode_cursor, encode_cursor, split_page
from messunjerr.identity.api_public import AccountCard, find_account
from messunjerr.profiles.api_public import UserSummary, load_owner_privacy
from messunjerr.social.domain.errors import list_hidden, profile_private
from messunjerr.social.domain.policies import ListAccess, decide_list_access
from messunjerr.social.infra.models import BlockRow, FollowRequestRow, FollowRow, is_follow_pending
from messunjerr.social.queries.cards import CARD_COLUMNS, Expr, summary_of, with_cards
from messunjerr.social.queries.follow_models import FollowRequest
from messunjerr.social.queries.models import UserListItem
from messunjerr.social.queries.relationships import SocialRelationships, relationships_for

_HIDDEN = "The user does not exist or is not available to you."


class FollowCursor(Cursor):
    """Ключ страницы списков подписчиков и подписок: дата подписки и человек напротив."""

    since: datetime
    id: uuid.UUID


class FollowRequestCursor(Cursor):
    """Ключ страницы входящих запросов: время запроса и его идентификатор (новые сверху)."""

    created_at: datetime
    id: uuid.UUID


def _hide_blocked[*Ts](statement: Select[*Ts], viewer_id: uuid.UUID, person: Expr) -> Select[*Ts]:
    """Убирает людей, с которыми у зрителя блокировка в любую сторону (то же условие, что у друзей)."""
    return statement.where(
        ~exists().where(
            or_(
                and_(BlockRow.blocker_id == viewer_id, BlockRow.blocked_id == person),
                and_(BlockRow.blocker_id == person, BlockRow.blocked_id == viewer_id),
            )
        )
    )


async def _follow_page(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    viewer_id: uuid.UUID,
    followers: bool,
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """Страница подписчиков (`followers`) или подписок владельца с карточкой человека напротив.

    Ключ `(дата подписки, человек)` по убыванию. Первый столбец пары PostgreSQL использует как
    границу диапазона индекса `ix_follows_followee` или `ix_follows_follower` (проверено `EXPLAIN` на
    6 000 подписчиках: глубокие страницы не перечитывают список с начала).
    """
    own, person = (
        (FollowRow.followee_id, FollowRow.follower_id)
        if followers
        else (FollowRow.follower_id, FollowRow.followee_id)
    )
    statement = with_cards(
        select(FollowRow.created_at.label("since"), *CARD_COLUMNS).where(own == owner_id), person
    )
    statement = _hide_blocked(statement, viewer_id, person)
    if cursor is not None:
        position = decode_cursor(cursor, FollowCursor)
        statement = statement.where(
            tuple_(FollowRow.created_at, person) < tuple_(position.since, position.id)
        )
    rows = (
        await session.execute(
            statement.order_by(FollowRow.created_at.desc(), person.desc()).limit(limit + 1)
        )
    ).all()
    page, has_more = split_page(rows, limit)
    next_cursor = (
        encode_cursor(FollowCursor(since=page[-1].since, id=page[-1].card_id)) if has_more else None
    )
    return page, next_cursor


async def list_my_follows(
    session: AsyncSession, *, user_id: uuid.UUID, followers: bool, limit: int, cursor: str | None
) -> Page[UserSummary]:
    """`GET /me/followers` (`followers=True`) и `GET /me/following`: плоские карточки, новые сверху."""
    rows, next_cursor = await _follow_page(
        session,
        owner_id=user_id,
        viewer_id=user_id,
        followers=followers,
        limit=limit,
        cursor=cursor,
    )
    return Page(items=[summary_of(row) for row in rows], next_cursor=next_cursor)


async def _readable_owner(session: AsyncSession, *, viewer_id: uuid.UUID, ref: str) -> AccountCard:
    """Владелец списка, если зритель вправе его видеть; иначе `404`, `403 profile_private` или
    `403 list_hidden`. Решает политика, здесь только чтение фактов."""
    account = await find_account(session, ref)
    if account is None:
        raise NotFoundError(_HIDDEN)
    link = await SocialRelationships().between(session, viewer_id=viewer_id, owner_id=account.id)
    privacy = await load_owner_privacy(session, account.id)
    if privacy is None:  # у аккаунта нет профиля: сбой данных, человека для других не существует
        raise NotFoundError(_HIDDEN)
    match decide_list_access(
        link.relation,
        owner_active=account.is_active,
        is_private=privacy.is_private,
        visibility=privacy.followers_list_visibility,
    ):
        case ListAccess.NOT_FOUND:
            raise NotFoundError(_HIDDEN)
        case ListAccess.PROFILE_PRIVATE:
            raise profile_private()
        case ListAccess.LIST_HIDDEN:
            raise list_hidden()
        case ListAccess.ALLOWED:
            return account


async def list_user_follows(
    session: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    ref: str,
    followers: bool,
    limit: int,
    cursor: str | None,
) -> Page[UserListItem]:
    """`GET /users/{ref}/followers` и `/following`: карточки с отношением зрителя к каждому."""
    account = await _readable_owner(session, viewer_id=viewer_id, ref=ref)
    rows, next_cursor = await _follow_page(
        session,
        owner_id=account.id,
        viewer_id=viewer_id,
        followers=followers,
        limit=limit,
        cursor=cursor,
    )
    relationships = await relationships_for(session, viewer_id, [row.card_id for row in rows])
    return Page(
        items=[
            UserListItem(**summary_of(row).model_dump(), relationship=relationships[row.card_id])
            for row in rows
        ],
        next_cursor=next_cursor,
    )


async def list_follow_requests(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int, cursor: str | None
) -> Page[FollowRequest]:
    """`GET /me/follow-requests`: ждущие запросы к человеку, новые сверху; от аккаунтов не `active`
    запросы не показываются (владельцу ответить им нечем: `404`)."""
    statement = with_cards(
        select(FollowRequestRow.id, FollowRequestRow.created_at, *CARD_COLUMNS).where(
            FollowRequestRow.followee_id == user_id, is_follow_pending()
        ),
        FollowRequestRow.follower_id,
    )
    if cursor is not None:
        position = decode_cursor(cursor, FollowRequestCursor)
        statement = statement.where(
            tuple_(FollowRequestRow.created_at, FollowRequestRow.id)
            < tuple_(position.created_at, position.id)
        )
    rows = (
        await session.execute(
            statement.order_by(
                FollowRequestRow.created_at.desc(), FollowRequestRow.id.desc()
            ).limit(limit + 1)
        )
    ).all()
    page, has_more = split_page(rows, limit)
    next_cursor = (
        encode_cursor(FollowRequestCursor(created_at=page[-1].created_at, id=page[-1].id))
        if has_more
        else None
    )
    return Page(
        items=[
            FollowRequest(id=row.id, user=summary_of(row), created_at=row.created_at)
            for row in page
        ],
        next_cursor=next_cursor,
    )

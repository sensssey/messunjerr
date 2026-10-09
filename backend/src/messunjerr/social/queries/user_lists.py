"""Списки друзей чужого профиля: `GET /users/{ref}/friends` и `/mutual-friends` (5.3).

Доступ решает политика (`decide_list_access`): блокировка, неактивный аккаунт и несуществующий человек
дают `404`, закрытый профиль `403 profile_private`, настройка владельца `403 list_hidden`. У общих друзей
ошибки только `404`: общие друзья по определению известны зрителю.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.errors import NotFoundError
from messunjerr.core.pagination import Page
from messunjerr.identity.api_public import AccountCard, find_account
from messunjerr.profiles.api_public import (
    Relation,
    UserSummary,
    can_view_profile,
    load_owner_privacy,
)
from messunjerr.social.domain.errors import list_hidden, profile_private
from messunjerr.social.domain.policies import ListAccess, decide_list_access
from messunjerr.social.queries.friends import list_mutual_friends, list_user_friends
from messunjerr.social.queries.models import UserListItem
from messunjerr.social.queries.relationships import SocialRelationships


async def _owner_and_relation(
    session: AsyncSession, *, viewer_id: uuid.UUID, ref: str
) -> tuple[AccountCard, Relation]:
    account = await find_account(session, ref)
    if account is None:
        raise NotFoundError("The user does not exist or is not available to you.")
    link = await SocialRelationships().between(session, viewer_id=viewer_id, owner_id=account.id)
    return account, link.relation


async def user_friends(
    session: AsyncSession, *, viewer_id: uuid.UUID, ref: str, limit: int, cursor: str | None
) -> Page[UserListItem]:
    account, relation = await _owner_and_relation(session, viewer_id=viewer_id, ref=ref)
    privacy = await load_owner_privacy(session, account.id)
    if privacy is None:  # у аккаунта нет профиля: сбой данных, человека для других не существует
        raise NotFoundError("The user does not exist or is not available to you.")
    match decide_list_access(
        relation,
        owner_active=account.is_active,
        is_private=privacy.is_private,
        visibility=privacy.friends_list_visibility,
    ):
        case ListAccess.NOT_FOUND:
            raise NotFoundError("The user does not exist or is not available to you.")
        case ListAccess.PROFILE_PRIVATE:
            raise profile_private()
        case ListAccess.LIST_HIDDEN:
            raise list_hidden()
        case ListAccess.ALLOWED:
            pass
    return await list_user_friends(
        session, owner_id=account.id, viewer_id=viewer_id, limit=limit, cursor=cursor
    )


async def user_mutual_friends(
    session: AsyncSession, *, viewer_id: uuid.UUID, ref: str, limit: int, cursor: str | None
) -> Page[UserSummary]:
    account, relation = await _owner_and_relation(session, viewer_id=viewer_id, ref=ref)
    if not can_view_profile(relation, owner_active=account.is_active):
        raise NotFoundError("The user does not exist or is not available to you.")
    return await list_mutual_friends(
        session, viewer_id=viewer_id, owner_id=account.id, limit=limit, cursor=cursor
    )

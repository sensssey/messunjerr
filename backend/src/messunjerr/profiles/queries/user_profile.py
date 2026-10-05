"""Запрос `GET /users/{ref}` (5.3): профиль человека глазами зрителя.

Какие поля отдать, решают политики (`profiles.domain.policies`); здесь только сборка ответа.
Нет такого пользователя, аккаунт не `active`, зритель и владелец в блокировке: везде одинаково `None`,
то есть `404` (4.6, «ресурс, о существовании которого зритель знать не должен»).
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import ProfileLink, avatar_for
from messunjerr.identity.api_public import find_account
from messunjerr.profiles.domain.errors import ProfileMissingError
from messunjerr.profiles.domain.policies import (
    BirthDateView,
    Relation,
    birth_date_view,
    can_see_counter,
    can_see_posts_counter,
    can_view_profile,
    sees_details,
)
from messunjerr.profiles.domain.ports import ProfileCounters, Relationships
from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow
from messunjerr.profiles.queries.models import (
    Relationship,
    UserCounters,
    UserProfile,
    UserSummary,
)


def render_birth_date(value: date | None, view: BirthDateView) -> str | None:
    """`1990-05-12` при полной видимости, `05-12` без года, иначе `null` (5.3)."""
    if value is None:
        return None
    match view:
        case BirthDateView.FULL:
            return value.isoformat()
        case BirthDateView.DAY_MONTH:
            return f"{value.month:02d}-{value.day:02d}"
        case BirthDateView.HIDDEN:
            return None


async def get_user_profile(
    session: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    ref: str,
    relationships: Relationships,
    counters: ProfileCounters,
) -> UserProfile | None:
    account = await find_account(session, ref)
    if account is None:
        return None
    link = await relationships.between(session, viewer_id=viewer_id, owner_id=account.id)
    relation = link.relation
    if not can_view_profile(relation, owner_active=account.is_active):
        return None

    row = (
        await session.execute(
            select(ProfileRow, PrivacySettingsRow)
            .join(PrivacySettingsRow, PrivacySettingsRow.user_id == ProfileRow.user_id)
            .where(ProfileRow.user_id == account.id)
        )
    ).one_or_none()
    if row is None:
        raise ProfileMissingError(account.id)
    profile, privacy = row
    counts = await counters.of(session, account.id)
    details = sees_details(relation, is_private=profile.is_private)
    birth = birth_date_view(
        relation, is_private=profile.is_private, visibility=profile.birth_date_visibility
    )
    return UserProfile(
        user=UserSummary(
            id=account.id,
            username=account.username,
            display_name=profile.display_name,
            avatar=avatar_for(profile.avatar_asset_id),
        ),
        bio=profile.bio,
        links=[ProfileLink(title=item["title"], url=item["url"]) for item in profile.links]
        if details
        else [],
        birth_date=render_birth_date(profile.birth_date, birth),
        city=profile.city if details else None,
        language=profile.language if details else None,
        timezone=profile.timezone if details else None,
        is_private=profile.is_private,
        created_at=account.created_at,
        counters=UserCounters(
            posts=counts.posts
            if can_see_posts_counter(relation, is_private=profile.is_private)
            else None,
            friends=counts.friends
            if can_see_counter(relation, visibility=privacy.friends_list_visibility)
            else None,
            followers=counts.followers
            if can_see_counter(relation, visibility=privacy.followers_list_visibility)
            else None,
            # Список подписок живёт под настройкой «кто видит подписчиков»: отдельной настройки нет.
            following=counts.following
            if can_see_counter(relation, visibility=privacy.followers_list_visibility)
            else None,
        ),
        relationship=Relationship(
            is_self=relation is Relation.SELF,
            friendship=link.friendship,
            friend_request_id=link.friend_request_id,
            following=link.following,
            follows_you=link.follows_you,
            blocked=link.blocked,
        ),
        presence=None,
    )

"""Счётчики социального графа: друзья, подписчики и подписки в профиле, входящие заявки и запросы
на подписку в шапке клиента.

Считаются при запросе (индексы по паре и получателю делают это дешёвым для списков в сотни
записей); отдельные счётчики-кэши появятся, если замеры S19 покажут нужду. Считаются только `active`
аккаунты: тот, кого скрывает список, не должен оставаться в числе.
"""

import uuid

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import MeCounters
from messunjerr.profiles.api_public import ProfileCounts
from messunjerr.social.infra.directory import ACTIVE, users
from messunjerr.social.infra.models import (
    FollowRequestRow,
    FollowRow,
    FriendRequestRow,
    FriendshipRow,
    is_follow_pending,
    is_pending,
)


async def count_friends(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Друзья человека, чьи аккаунты `active`."""
    friend_of = case(
        (FriendshipRow.user_low_id == user_id, FriendshipRow.user_high_id),
        else_=FriendshipRow.user_low_id,
    )
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FriendshipRow)
                .join(users, users.c.id == friend_of)
                .where(
                    or_(
                        FriendshipRow.user_low_id == user_id, FriendshipRow.user_high_id == user_id
                    ),
                    users.c.status == ACTIVE,
                )
            )
        ).scalar_one()
    )


async def count_incoming_requests(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Активные заявки, адресованные человеку, от `active` аккаунтов."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FriendRequestRow)
                .join(users, users.c.id == FriendRequestRow.sender_id)
                .where(
                    FriendRequestRow.receiver_id == user_id,
                    is_pending(),
                    users.c.status == ACTIVE,
                )
            )
        ).scalar_one()
    )


async def count_followers(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Подписчики человека, чьи аккаунты `active`."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FollowRow)
                .join(users, users.c.id == FollowRow.follower_id)
                .where(FollowRow.followee_id == user_id, users.c.status == ACTIVE)
            )
        ).scalar_one()
    )


async def count_following(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Подписки человека на аккаунты `active`."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FollowRow)
                .join(users, users.c.id == FollowRow.followee_id)
                .where(FollowRow.follower_id == user_id, users.c.status == ACTIVE)
            )
        ).scalar_one()
    )


async def count_incoming_follow_requests(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Ждущие запросы на подписку, адресованные человеку, от `active` аккаунтов."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FollowRequestRow)
                .join(users, users.c.id == FollowRequestRow.follower_id)
                .where(
                    FollowRequestRow.followee_id == user_id,
                    is_follow_pending(),
                    users.c.status == ACTIVE,
                )
            )
        ).scalar_one()
    )


class SocialCounters:
    """Порт `ProfileCounters`: друзья, подписчики и подписки (посты добавит S11)."""

    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> ProfileCounts:
        return ProfileCounts(
            friends=await count_friends(session, user_id),
            followers=await count_followers(session, user_id),
            following=await count_following(session, user_id),
        )


class SocialMeCounters:
    """Порт `MeCountersSource`: заявки в друзья и запросы на подписку, которые ждут ответа."""

    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> MeCounters:
        return MeCounters(
            pending_friend_requests=await count_incoming_requests(session, user_id),
            pending_follow_requests=await count_incoming_follow_requests(session, user_id),
        )

"""Отношения зрителя к человеку (5.1, `Relationship`): единственное место, где они собираются из таблиц графа.

`SocialRelationships` реализует порт `Relationships` профилей (`GET /users/{ref}`), а
`relationships_for` собирает то же для целой страницы списка за четыре запроса. Подписки (S8):
`following` это отношение зрителя к человеку (`following`, если подписка подтверждена, `requested`,
если запрос ждёт ответа), `follows_you` значит, что человек подписан на зрителя.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.profiles.api_public import Following, Relationship, RelationshipView
from messunjerr.social.domain.rules import ordered_pair
from messunjerr.social.infra.models import (
    BlockRow,
    FollowRequestRow,
    FollowRow,
    FriendRequestRow,
    FriendshipRow,
    is_follow_pending,
    is_pending,
)


class SocialRelationships:
    """Порт `Relationships` профилей: как зритель связан с владельцем профиля."""

    async def between(
        self, session: AsyncSession, *, viewer_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RelationshipView:
        if viewer_id == owner_id:
            return RelationshipView(is_self=True)
        low, high = ordered_pair(viewer_id, owner_id)
        facts = (
            await session.execute(
                select(
                    exists()
                    .where(FriendshipRow.user_low_id == low, FriendshipRow.user_high_id == high)
                    .label("friends"),
                    exists()
                    .where(BlockRow.blocker_id == viewer_id, BlockRow.blocked_id == owner_id)
                    .label("blocked"),
                    exists()
                    .where(BlockRow.blocker_id == owner_id, BlockRow.blocked_id == viewer_id)
                    .label("blocked_by_owner"),
                    exists()
                    .where(FollowRow.follower_id == viewer_id, FollowRow.followee_id == owner_id)
                    .label("following"),
                    exists()
                    .where(
                        is_follow_pending(),
                        FollowRequestRow.follower_id == viewer_id,
                        FollowRequestRow.followee_id == owner_id,
                    )
                    .label("requested"),
                    exists()
                    .where(FollowRow.follower_id == owner_id, FollowRow.followee_id == viewer_id)
                    .label("follows_you"),
                )
            )
        ).one()
        following: Following = (
            "following" if facts.following else "requested" if facts.requested else "none"
        )
        follows_you = bool(facts.follows_you)
        if facts.friends or facts.blocked or facts.blocked_by_owner:
            return RelationshipView(
                is_self=False,
                friendship="friends" if facts.friends else "none",
                following=following,
                follows_you=follows_you,
                blocked=bool(facts.blocked),
                blocked_by_owner=bool(facts.blocked_by_owner),
            )
        pending = (
            await session.execute(
                select(FriendRequestRow.id, FriendRequestRow.sender_id).where(
                    is_pending(),
                    func.least(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == low,
                    func.greatest(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == high,
                )
            )
        ).one_or_none()
        if pending is None:
            return RelationshipView(is_self=False, following=following, follows_you=follows_you)
        return RelationshipView(
            is_self=False,
            friendship="request_sent" if pending.sender_id == viewer_id else "request_received",
            friend_request_id=pending.id,
            following=following,
            follows_you=follows_you,
        )


async def relationships_for(
    session: AsyncSession, viewer_id: uuid.UUID, other_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Relationship]:
    """`Relationship` зрителя к каждому из `other_ids` (для страницы списка людей).

    Блокировок здесь нет: людей, связанных со зрителем блокировкой, списки не показывают вовсе.
    """
    if not other_ids:
        return {}
    wanted = list(other_ids)
    friend_of = case(
        (FriendshipRow.user_low_id == viewer_id, FriendshipRow.user_high_id),
        else_=FriendshipRow.user_low_id,
    )
    friends = set(
        (
            await session.execute(
                select(friend_of).where(
                    or_(
                        and_(
                            FriendshipRow.user_low_id == viewer_id,
                            FriendshipRow.user_high_id.in_(wanted),
                        ),
                        and_(
                            FriendshipRow.user_high_id == viewer_id,
                            FriendshipRow.user_low_id.in_(wanted),
                        ),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    pending_rows = (
        await session.execute(
            select(
                FriendRequestRow.id, FriendRequestRow.sender_id, FriendRequestRow.receiver_id
            ).where(
                is_pending(),
                or_(
                    and_(
                        FriendRequestRow.sender_id == viewer_id,
                        FriendRequestRow.receiver_id.in_(wanted),
                    ),
                    and_(
                        FriendRequestRow.receiver_id == viewer_id,
                        FriendRequestRow.sender_id.in_(wanted),
                    ),
                ),
            )
        )
    ).all()
    sent: dict[uuid.UUID, uuid.UUID] = {}
    received: dict[uuid.UUID, uuid.UUID] = {}
    for row in pending_rows:
        if row.sender_id == viewer_id:
            sent[row.receiver_id] = row.id
        else:
            received[row.sender_id] = row.id

    follow_rows = (
        await session.execute(
            select(FollowRow.follower_id, FollowRow.followee_id).where(
                or_(
                    and_(FollowRow.follower_id == viewer_id, FollowRow.followee_id.in_(wanted)),
                    and_(FollowRow.followee_id == viewer_id, FollowRow.follower_id.in_(wanted)),
                )
            )
        )
    ).all()
    following = {row.followee_id for row in follow_rows if row.follower_id == viewer_id}
    followed_by = {row.follower_id for row in follow_rows if row.followee_id == viewer_id}
    requested = set(
        (
            await session.execute(
                select(FollowRequestRow.followee_id).where(
                    is_follow_pending(),
                    FollowRequestRow.follower_id == viewer_id,
                    FollowRequestRow.followee_id.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )

    result: dict[uuid.UUID, Relationship] = {}
    for other in wanted:
        if other == viewer_id:
            result[other] = Relationship(
                is_self=True,
                friendship="none",
                friend_request_id=None,
                following="none",
                follows_you=False,
                blocked=False,
            )
            continue
        if other in friends:
            friendship, request_id = "friends", None
        elif other in sent:
            friendship, request_id = "request_sent", sent[other]
        elif other in received:
            friendship, request_id = "request_received", received[other]
        else:
            friendship, request_id = "none", None
        result[other] = Relationship(
            is_self=False,
            friendship=friendship,
            friend_request_id=request_id,
            following="following"
            if other in following
            else "requested"
            if other in requested
            else "none",
            follows_you=other in followed_by,
            blocked=False,
        )
    return result

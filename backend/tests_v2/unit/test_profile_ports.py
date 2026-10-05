"""Порты профилей: сведение отношений к матрице 4.6, заглушки до S6–S8, адреса аватара."""

import uuid
from typing import Any

import pytest

from messunjerr.core.me import MeCounters, avatar_for
from messunjerr.profiles.domain.policies import Relation
from messunjerr.profiles.domain.ports import AvatarCheck, ProfileCounts, RelationshipView
from messunjerr.profiles.infra.stubs import NoGraphYet, NoMediaYet, ZeroCounters

A, B = uuid.uuid4(), uuid.uuid4()
SESSION: Any = None  # заглушки к БД не обращаются


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (RelationshipView(is_self=True), Relation.SELF),
        (RelationshipView(is_self=False), Relation.STRANGER),
        (RelationshipView(is_self=False, friendship="friends"), Relation.FRIEND),
        (RelationshipView(is_self=False, following="following"), Relation.FOLLOWER),
        (RelationshipView(is_self=False, following="requested"), Relation.STRANGER),
        (RelationshipView(is_self=False, friendship="request_sent"), Relation.STRANGER),
        (RelationshipView(is_self=False, friendship="request_received"), Relation.STRANGER),
        (RelationshipView(is_self=False, follows_you=True), Relation.STRANGER),
        (RelationshipView(is_self=False, blocked=True), Relation.BLOCKED),
        (RelationshipView(is_self=False, blocked_by_owner=True), Relation.BLOCKED),
        # блокировка сильнее дружбы и подписки (граф их снимает, но порядок проверок страхует)
        (
            RelationshipView(is_self=False, friendship="friends", blocked=True),
            Relation.BLOCKED,
        ),
        (
            RelationshipView(is_self=False, following="following", blocked_by_owner=True),
            Relation.BLOCKED,
        ),
        # дружба сильнее подписки
        (
            RelationshipView(is_self=False, friendship="friends", following="following"),
            Relation.FRIEND,
        ),
    ],
)
def test_a_relationship_collapses_to_the_policy_state(
    view: RelationshipView, expected: Relation
) -> None:
    assert view.relation is expected


async def test_without_a_graph_the_viewer_is_the_owner_or_a_stranger() -> None:
    graph = NoGraphYet()

    own = await graph.between(SESSION, viewer_id=A, owner_id=A)
    other = await graph.between(SESSION, viewer_id=A, owner_id=B)

    assert own.relation is Relation.SELF
    assert other == RelationshipView(is_self=False)
    assert other.relation is Relation.STRANGER


async def test_without_media_every_avatar_is_not_found() -> None:
    assert await NoMediaYet().check(SESSION, owner_id=A, asset_id=B) is AvatarCheck.NOT_FOUND


async def test_without_a_graph_everything_counts_zero() -> None:
    assert await ZeroCounters().of(SESSION, A) == ProfileCounts(0, 0, 0, 0)


def test_avatar_urls_follow_the_public_media_layout() -> None:
    asset = uuid.UUID("0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80")
    avatar = avatar_for(asset)
    assert avatar is not None
    assert avatar.sm == f"/media/public/avatars/{asset}/64.webp"
    assert avatar.md == f"/media/public/avatars/{asset}/256.webp"
    assert avatar_for(None) is None


def test_me_counters_default_to_zero_and_cannot_be_negative() -> None:
    assert MeCounters().model_dump() == {
        "unread_notifications": 0,
        "unread_conversations": 0,
        "pending_friend_requests": 0,
        "pending_follow_requests": 0,
    }
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        MeCounters(unread_notifications=-1)

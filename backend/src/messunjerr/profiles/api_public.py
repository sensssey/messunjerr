"""Публичный интерфейс profiles для контекстов выше (4.2, правило 1).

Социальный граф, контент и чат берут отсюда карточку человека, состояния отношений, политики
видимости и порты, которые реализуют сами; модели, репозитории и команды profiles они не
импортируют (контракт в `.importlinter`).
"""

from messunjerr.profiles.domain.policies import (
    Relation,
    can_see_counter,
    can_see_list,
    can_view_profile,
    sees_details,
)
from messunjerr.profiles.domain.ports import (
    AvatarAssets,
    AvatarCheck,
    Following,
    Friendship,
    MeCountersSource,
    ProfileCounters,
    ProfileCounts,
    ProfileVisibilityListener,
    RelationshipView,
)
from messunjerr.profiles.queries.models import Relationship, UserSummary
from messunjerr.profiles.queries.owner_privacy import OwnerPrivacy, load_owner_privacy

__all__ = [
    "AvatarAssets",
    "AvatarCheck",
    "Following",
    "Friendship",
    "MeCountersSource",
    "OwnerPrivacy",
    "ProfileCounters",
    "ProfileCounts",
    "ProfileVisibilityListener",
    "Relation",
    "Relationship",
    "RelationshipView",
    "UserSummary",
    "can_see_counter",
    "can_see_list",
    "can_view_profile",
    "load_owner_privacy",
    "sees_details",
]

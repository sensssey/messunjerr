"""Команда `PATCH /me/privacy` (5.3, S3-02): настройки приватности владельца."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from messunjerr.core.me import PrivacySettings
from messunjerr.core.uow import UnitOfWork
from messunjerr.profiles.domain.errors import ProfileMissingError
from messunjerr.profiles.infra.repositories import PrivacyRepository
from messunjerr.profiles.queries.me import privacy_dto

PRIVACY_FIELDS = frozenset(
    {
        "dm_policy",
        "comment_policy",
        "mention_policy",
        "friends_list_visibility",
        "followers_list_visibility",
        "presence_visibility",
        "default_post_visibility",
    }
)


@dataclass(frozen=True, slots=True)
class UpdatePrivacy:
    user_id: uuid.UUID
    changes: Mapping[str, str]
    """Только присланные поля (имена из `PRIVACY_FIELDS`), значения из перечислений схемы."""


async def update_privacy(command: UpdatePrivacy, *, uow: UnitOfWork) -> PrivacySettings:
    """Применяет изменения и возвращает настройки целиком."""
    row = await PrivacyRepository(uow.session).get(command.user_id, for_update=True)
    if row is None:
        raise ProfileMissingError(command.user_id)
    for name, value in command.changes.items():
        if name not in PRIVACY_FIELDS:
            raise ValueError(f"unknown privacy field: {name}")
        setattr(row, name, value)
    await uow.commit()
    return privacy_dto(row)

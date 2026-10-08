"""Команда `PATCH /me/profile` (5.3, S3-02).

Как JSON Merge Patch (5.1): отсутствующий ключ значит «без изменений», `null` очищает поле, если это
допустимо. Роутер передаёт только присланные поля (`UpdateProfileRequest.to_changes`), команда
проверяет то, что требует часов, настроек и других контекстов:

- возраст по `birth_date` (`MIN_AGE`, код `underage`; дата в будущем и неправдоподобная `out_of_range`);
- `avatar_asset_id` через порт медиа (`asset_not_found`, `asset_not_ready`, `asset_wrong_purpose`);
  замена и очистка аватара освобождают прежний ресурс (он удаляется вместе с объектами).

Все найденные ошибки приходят одним ответом `422`.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import ErrorItem, ValidationFailedError
from messunjerr.core.me import MeProfile
from messunjerr.core.uow import UnitOfWork
from messunjerr.profiles.domain.errors import (
    ProfileMissingError,
    avatar_error,
    birth_date_error,
)
from messunjerr.profiles.domain.ports import AvatarAssets, AvatarCheck
from messunjerr.profiles.domain.rules import check_birth_date
from messunjerr.profiles.infra.repositories import ProfileRepository
from messunjerr.profiles.queries.me import profile_dto
from messunjerr.settings import Settings

PROFILE_FIELDS = frozenset(
    {
        "display_name",
        "bio",
        "links",
        "birth_date",
        "birth_date_visibility",
        "city",
        "language",
        "timezone",
        "is_private",
        "avatar_asset_id",
    }
)
_EMPTY_IS_NULL = frozenset({"bio", "city"})
"""Пустая строка в этих полях значит «очистить»: хранится `NULL`, а не `''`."""


@dataclass(frozen=True, slots=True)
class UpdateProfile:
    user_id: uuid.UUID
    changes: Mapping[str, Any]
    """Только присланные поля (имена из `PROFILE_FIELDS`), значения уже разобраны схемой."""


async def _validate(
    command: UpdateProfile,
    *,
    uow: UnitOfWork,
    settings: Settings,
    avatars: AvatarAssets,
    today: date,
) -> None:
    errors: list[ErrorItem] = []
    birth_date: date | None = command.changes.get("birth_date")
    if birth_date is not None:
        problem = check_birth_date(birth_date, today=today, min_age=settings.min_age)
        if problem is not None:
            errors.append(birth_date_error(problem, min_age=settings.min_age))
    asset_id: uuid.UUID | None = command.changes.get("avatar_asset_id")
    if asset_id is not None:
        check = await avatars.check(uow.session, owner_id=command.user_id, asset_id=asset_id)
        if check is not AvatarCheck.OK:
            errors.append(avatar_error(check))
    if errors:
        raise ValidationFailedError(errors)


async def update_profile(
    command: UpdateProfile,
    *,
    uow: UnitOfWork,
    settings: Settings,
    avatars: AvatarAssets,
    now: datetime | None = None,
) -> MeProfile:
    """Применяет изменения и возвращает профиль владельца. Строка блокируется: правки не теряются."""
    moment = now or utcnow()
    row = await ProfileRepository(uow.session).get(command.user_id, for_update=True)
    if row is None:
        raise ProfileMissingError(command.user_id)
    await _validate(command, uow=uow, settings=settings, avatars=avatars, today=moment.date())

    previous_avatar = row.avatar_asset_id
    for name, value in command.changes.items():
        if name not in PROFILE_FIELDS:
            raise ValueError(f"unknown profile field: {name}")
        setattr(row, name, (value or None) if name in _EMPTY_IS_NULL else value)
    if previous_avatar is not None and previous_avatar != row.avatar_asset_id:
        await avatars.release(uow, owner_id=command.user_id, asset_id=previous_avatar)
    await uow.commit()
    return profile_dto(row)

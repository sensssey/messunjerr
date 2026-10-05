"""Запросы чтения: текущий пользователь и доступность ника (SQL без ORM-связей, 4.3)."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.identity.domain.usernames import UsernameProblem, check_username, normalize_username
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.queries.models import MeUser


async def get_me(session: AsyncSession, user_id: uuid.UUID) -> MeUser | None:
    user = (
        await session.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    return MeUser.from_row(user) if user is not None else None


class UsernameAvailability(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    reason: Literal["taken", "reserved", "invalid"] | None = None


async def check_username_available(
    session: AsyncSession, raw_username: str
) -> UsernameAvailability:
    """`GET /auth/username-available`: формат и список зарезервированных, затем занятость."""
    username = normalize_username(raw_username)
    match check_username(username):
        case UsernameProblem.INVALID:
            return UsernameAvailability(available=False, reason="invalid")
        case UsernameProblem.RESERVED:
            return UsernameAvailability(available=False, reason="reserved")
        case None:
            pass
    taken = await session.scalar(select(exists().where(UserRow.username == username)))
    if taken:
        return UsernameAvailability(available=False, reason="taken")
    return UsernameAvailability(available=True)

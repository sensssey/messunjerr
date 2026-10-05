"""Запросы чтения: текущий пользователь и доступность ника (SQL без ORM-связей, 4.3)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.clock import utcnow
from messunjerr.identity.domain.usernames import UsernameProblem, check_username, normalize_username
from messunjerr.identity.infra.models import UsernameReservationRow, UserRow
from messunjerr.identity.infra.ports import MeExtrasProvider
from messunjerr.identity.queries.models import MeUser


async def build_me(session: AsyncSession, user: UserRow, extras: MeExtrasProvider) -> MeUser:
    """`MeUser` по строке пользователя: разделы профиля, приватности и счётчиков берутся из порта."""
    return MeUser.from_row(user, await extras.load(session, user.id))


async def get_me(
    session: AsyncSession, user_id: uuid.UUID, extras: MeExtrasProvider
) -> MeUser | None:
    user = (
        await session.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    return await build_me(session, user, extras) if user is not None else None


class UsernameAvailability(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    reason: Literal["taken", "reserved", "invalid"] | None = None


async def username_is_reserved(
    session: AsyncSession,
    username: str,
    *,
    now: datetime | None = None,
    except_user_id: uuid.UUID | None = None,
) -> bool:
    """Держит ли ник чей-то резерв после смены (5.3). Резерв самого `except_user_id` не в счёт:
    свой прежний ник человек может вернуть."""
    holder = await session.scalar(
        select(UsernameReservationRow.user_id).where(
            UsernameReservationRow.username == username,
            UsernameReservationRow.reserved_until > (now or utcnow()),
        )
    )
    return holder is not None and holder != except_user_id


async def username_is_held(
    session: AsyncSession,
    username: str,
    *,
    now: datetime | None = None,
    except_user_id: uuid.UUID | None = None,
) -> bool:
    """Занят ли ник: принадлежит аккаунту или резервируется после чьей-то смены (5.3).

    Порядок запросов важен. Смена ника в одной транзакции освобождает ник и ставит резерв; читая
    сначала владельца, потом резерв, мы либо видим владельца, либо (после коммита) видим резерв,
    но не промежуток между ними.
    """
    owner = await session.scalar(select(UserRow.id).where(UserRow.username == username))
    if owner is not None:
        return True
    return await username_is_reserved(session, username, now=now, except_user_id=except_user_id)


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
    if await username_is_held(session, username):
        return UsernameAvailability(available=False, reason="taken")
    return UsernameAvailability(available=True)

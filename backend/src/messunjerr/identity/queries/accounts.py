"""Карточка учётной записи для других контекстов: кто это, жив ли аккаунт (через `api_public`)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.identity.domain.usernames import parse_user_ref
from messunjerr.identity.infra.models import UserRow


@dataclass(frozen=True, slots=True)
class AccountCard:
    """Минимум об аккаунте, нужный профилям, социальному графу и контенту."""

    id: uuid.UUID
    username: str
    status: str
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == "active"


async def find_account(session: AsyncSession, ref: str) -> AccountCard | None:
    """Аккаунт по UUID или нику (`{ref}` из путей `/users/{ref}`); `None`, если такого нет."""
    parsed = parse_user_ref(ref)
    if parsed is None:
        return None
    condition = (
        UserRow.id == parsed if isinstance(parsed, uuid.UUID) else UserRow.username == parsed
    )
    row = (
        await session.execute(
            select(UserRow.id, UserRow.username, UserRow.status, UserRow.created_at).where(
                condition
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return AccountCard(
        id=row.id, username=row.username, status=row.status, created_at=row.created_at
    )

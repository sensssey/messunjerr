"""Карточки людей в запросах графа: общий фрагмент соединения и сборка `UserSummary`.

Списки берут карточки одним соединением с `identity.users` и `profile.profiles` (см. `infra.directory`).
Аккаунт, который не `active`, другим не виден (4.6): `only_active` убирает его уже в запросе, чтобы
страницы не пустели после фильтра.
"""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from messunjerr.core.me import avatar_for
from messunjerr.profiles.api_public import UserSummary
from messunjerr.social.infra.directory import ACTIVE, profiles, users

# Идентификатор человека называется `card_id`, чтобы не сталкиваться с `id` заявки или связи рядом.
CARD_COLUMNS = (
    users.c.id.label("card_id"),
    users.c.username,
    profiles.c.display_name,
    profiles.c.avatar_asset_id,
)


type Expr = ColumnElement[Any] | InstrumentedAttribute[Any]
"""Выражение-столбец: колонка модели или колонка подзапроса."""


def with_cards[*Ts](
    statement: Select[*Ts], user_id: Expr, *, only_active: bool = True
) -> Select[*Ts]:
    """Присоединяет карточку человека `user_id`; по умолчанию скрывает аккаунты не `active`."""
    joined = statement.join(users, users.c.id == user_id).join(
        profiles, profiles.c.user_id == users.c.id
    )
    return joined.where(users.c.status == ACTIVE) if only_active else joined


def summary_of(row: Any) -> UserSummary:
    """Карточка из строки с колонками `CARD_COLUMNS`."""
    return UserSummary(
        id=row.card_id,
        username=row.username,
        display_name=row.display_name,
        avatar=avatar_for(row.avatar_asset_id),
    )


async def load_summaries(
    session: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, UserSummary]:
    """Карточки по идентификаторам (без фильтра по статусу: вызывающий уже решил, кого показывать)."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(*CARD_COLUMNS)
            .select_from(users)
            .join(profiles, profiles.c.user_id == users.c.id)
            .where(users.c.id.in_(ids))
        )
    ).all()
    return {row.card_id: summary_of(row) for row in rows}

"""Таблицы схемы `profile` (4.5): профили и настройки приватности.

Внешние ключи на `identity.users` и `media.assets` объявлены строками: таблицы регистрируют модули
моделей identity и media, которые импортируют приложение, воркеры, `seeding` и `migrations/env.py`
(profiles стоит ниже media в графе контекстов и импортировать его не может). Любой процесс, который
пишет в `profile.profiles`, обязан импортировать и `messunjerr.media.infra.models`: иначе SQLAlchemy
на сбросе изменений не найдёт таблицу ключа.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from messunjerr.core.db import Base
from messunjerr.core.me import (
    AUDIENCES,
    BIRTH_DATE_VISIBILITIES,
    LIST_VISIBILITIES,
    POST_VISIBILITIES,
    Audience,
    BirthDateVisibility,
    ListVisibility,
    PostVisibility,
)

PROFILE_SCHEMA = "profile"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class ProfileRow(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 50", name="display_name_length"),
        CheckConstraint("char_length(bio) <= 500", name="bio_length"),
        CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 5", name="links_shape"
        ),
        CheckConstraint(
            _in("birth_date_visibility", BIRTH_DATE_VISIBILITIES), name="birth_date_visibility"
        ),
        CheckConstraint("char_length(city) <= 100", name="city_length"),
        CheckConstraint("language ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'", name="language_format"),
        {"schema": PROFILE_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)
    links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    birth_date: Mapped[date | None] = mapped_column(Date)
    birth_date_visibility: Mapped[BirthDateVisibility] = mapped_column(
        Text, server_default=text("'hidden'")
    )
    city: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(server_default=text("false"))
    avatar_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("media.assets.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PrivacySettingsRow(Base):
    __tablename__ = "privacy_settings"
    __table_args__ = (
        CheckConstraint(_in("dm_policy", AUDIENCES), name="dm_policy"),
        CheckConstraint(_in("comment_policy", AUDIENCES), name="comment_policy"),
        CheckConstraint(_in("mention_policy", AUDIENCES), name="mention_policy"),
        CheckConstraint(
            _in("friends_list_visibility", LIST_VISIBILITIES), name="friends_list_visibility"
        ),
        CheckConstraint(
            _in("followers_list_visibility", LIST_VISIBILITIES), name="followers_list_visibility"
        ),
        CheckConstraint(_in("presence_visibility", AUDIENCES), name="presence_visibility"),
        CheckConstraint(
            _in("default_post_visibility", POST_VISIBILITIES), name="default_post_visibility"
        ),
        {"schema": PROFILE_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    dm_policy: Mapped[Audience] = mapped_column(Text, server_default=text("'friends'"))
    comment_policy: Mapped[Audience] = mapped_column(Text, server_default=text("'everyone'"))
    mention_policy: Mapped[Audience] = mapped_column(Text, server_default=text("'everyone'"))
    friends_list_visibility: Mapped[ListVisibility] = mapped_column(
        Text, server_default=text("'friends'")
    )
    followers_list_visibility: Mapped[ListVisibility] = mapped_column(
        Text, server_default=text("'friends'")
    )
    presence_visibility: Mapped[Audience] = mapped_column(Text, server_default=text("'friends'"))
    default_post_visibility: Mapped[PostVisibility] = mapped_column(
        Text, server_default=text("'friends'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

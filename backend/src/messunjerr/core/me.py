"""Контракт «я» (5.3): разделы `MeUser`, которые собирают несколько контекстов.

Форма ответа общая для `GET /me` и ответов входа (`/auth/login`, `/auth/refresh`, `/auth/verify-email`),
а данные разделов принадлежат разным контекстам: профиль и приватность профилям, счётчики
уведомлений, друзей и бесед контекстам выше (S7, S10, S14). Контекст identity стоит ниже них и их не
импортирует, поэтому сами модели лежат в `core`, а данные ему приносит порт `MeExtrasProvider`
(`identity/infra/ports.py`), который реализуют верхние контексты.
"""

import uuid
from datetime import date
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

BirthDateVisibility = Literal["hidden", "day_month", "full"]
Audience = Literal["everyone", "friends", "nobody"]
"""Кому разрешено: личные сообщения, комментарии, упоминания, статус «в сети»."""
ListVisibility = Literal["everyone", "friends", "only_me"]
PostVisibility = Literal["public", "friends", "private"]

# Перечисления для проверок в БД и для разбора запросов: один источник, как в DDL 4.5.
BIRTH_DATE_VISIBILITIES: tuple[str, ...] = get_args(BirthDateVisibility)
AUDIENCES: tuple[str, ...] = get_args(Audience)
LIST_VISIBILITIES: tuple[str, ...] = get_args(ListVisibility)
POST_VISIBILITIES: tuple[str, ...] = get_args(PostVisibility)


class Avatar(BaseModel):
    """Адреса аватара двух размеров; публичны и кэшируются навсегда (5.1)."""

    model_config = ConfigDict(frozen=True)

    sm: str = Field(examples=["/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp"])
    md: str = Field(
        examples=["/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp"]
    )


def avatar_for(asset_id: uuid.UUID | None) -> Avatar | None:
    """Адреса аватара по идентификатору ресурса; `None`, если аватар не задан (5.1)."""
    if asset_id is None:
        return None
    base = f"/media/public/avatars/{asset_id}"
    return Avatar(sm=f"{base}/64.webp", md=f"{base}/256.webp")


class ProfileLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(examples=["Блог"])
    url: str = Field(examples=["https://example.com"])


class MeProfile(BaseModel):
    """Профиль владельца: все поля, как он их заполнил (чужим они отдаются по правилам 4.6)."""

    # В примере нет `null`: FastAPI выбрасывает их из схемы OpenAPI, а ключи в ответе всегда есть.
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "display_name": "Иван",
                    "avatar": {
                        "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
                        "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
                    },
                    "bio": "Люблю горы",
                    "links": [{"title": "Блог", "url": "https://example.com"}],
                    "birth_date": "1990-05-12",
                    "birth_date_visibility": "day_month",
                    "city": "Казань",
                    "language": "ru",
                    "timezone": "Europe/Moscow",
                    "is_private": False,
                    "hidden_fields": [],
                }
            ]
        },
    )

    display_name: str
    avatar: Avatar | None
    bio: str | None
    links: list[ProfileLink]
    birth_date: date | None
    birth_date_visibility: BirthDateVisibility
    city: str | None
    language: str | None
    timezone: str | None
    is_private: bool
    hidden_fields: list[str] = Field(
        description=(
            "⚖️ Поля, которые заполнены, но другим не видны, потому что их категория не входит в "
            "согласие на распространение. В v1 всегда пустой (полный реестр согласий в бэклоге, B-01)."
        )
    )


class PrivacySettings(BaseModel):
    """Настройки приватности владельца (4.5, 4.6)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "dm_policy": "friends",
                    "comment_policy": "everyone",
                    "mention_policy": "everyone",
                    "friends_list_visibility": "friends",
                    "followers_list_visibility": "friends",
                    "presence_visibility": "friends",
                    "default_post_visibility": "friends",
                }
            ]
        },
    )

    dm_policy: Audience
    comment_policy: Audience
    mention_policy: Audience
    friends_list_visibility: ListVisibility
    followers_list_visibility: ListVisibility
    presence_visibility: Audience
    default_post_visibility: PostVisibility


class MeCounters(BaseModel):
    """Счётчики для шапки клиента. До S7, S10 и S14 нулевые: откуда им взяться, ещё нет."""

    model_config = ConfigDict(frozen=True)

    unread_notifications: Annotated[int, Field(ge=0)] = 0
    unread_conversations: Annotated[int, Field(ge=0)] = 0
    pending_friend_requests: Annotated[int, Field(ge=0)] = 0
    pending_follow_requests: Annotated[int, Field(ge=0)] = 0


class RequiredAction(BaseModel):
    """⚖️ Действие, без которого доступ ограничен (5.3). В v1 список всегда пуст (бэклог B-01)."""

    model_config = ConfigDict(frozen=True)

    type: Literal["accept_document", "confirm_age", "choose_username", "verify_email"]
    slug: str | None = None
    version: str | None = None


class MeExtras(BaseModel):
    """Всё, что `MeUser` берёт не из учётной записи: профиль, приватность, счётчики."""

    model_config = ConfigDict(frozen=True)

    profile: MeProfile
    privacy: PrivacySettings
    counters: MeCounters
    required_actions: list[RequiredAction]

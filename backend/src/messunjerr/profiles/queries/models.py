"""Модели чтения контекста profiles: неизменяемые, формат из спецификации (5.1, 5.3)."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from messunjerr.core.me import Avatar, ProfileLink
from messunjerr.core.schemas import UtcDateTime
from messunjerr.profiles.domain.ports import Following, Friendship


class UserSummary(BaseModel):
    """Краткая карточка человека (5.1): её показывают списки друзей, посты, чат."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    username: str
    display_name: str
    avatar: Avatar | None


class Relationship(BaseModel):
    """Как зритель связан с владельцем (5.1). Факт блокировки **вас** не раскрывается: тогда `404`."""

    model_config = ConfigDict(frozen=True)

    is_self: bool
    friendship: Friendship
    friend_request_id: uuid.UUID | None
    following: Following
    follows_you: bool
    blocked: bool


class UserCounters(BaseModel):
    """Счётчики профиля; `null` означает «вам не показывают» (настройки владельца, закрытый профиль)."""

    model_config = ConfigDict(frozen=True)

    posts: int | None
    friends: int | None
    followers: int | None
    following: int | None


class Presence(BaseModel):
    """Статус «в сети» (4.9, S16). Пока присутствия нет, поле профиля всегда `null`."""

    model_config = ConfigDict(frozen=True)

    online: bool
    last_seen_at: UtcDateTime | None


class UserProfile(BaseModel):
    """Профиль человека глазами зрителя (5.3): чего зритель видеть не должен, в нём `null` или пусто."""

    # В примере нет `null`: FastAPI выбрасывает их из схемы OpenAPI, а ключи в ответе всегда есть.
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "user": {
                        "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
                        "username": "anna",
                        "display_name": "Анна",
                        "avatar": {
                            "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
                            "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
                        },
                    },
                    "bio": "Люблю горы",
                    "links": [{"title": "Блог", "url": "https://example.com"}],
                    "birth_date": "05-12",
                    "city": "Казань",
                    "language": "ru",
                    "timezone": "Europe/Moscow",
                    "is_private": False,
                    "created_at": "2026-10-05T12:34:56.789Z",
                    "counters": {"posts": 42, "friends": 17, "followers": 90, "following": 31},
                    "relationship": {
                        "is_self": False,
                        "friendship": "request_received",
                        "friend_request_id": "0192b7a1-0000-7c3a-9d54-3f1a2b6c7d81",
                        "following": "none",
                        "follows_you": False,
                        "blocked": False,
                    },
                    "presence": {"online": True, "last_seen_at": "2026-10-05T12:30:00.000Z"},
                }
            ]
        },
    )

    user: UserSummary
    bio: str | None
    links: list[ProfileLink]
    birth_date: str | None = Field(
        description='`"1990-05-12"` при видимости `full`, `"05-12"` при `day_month`, иначе `null`.'
    )
    city: str | None
    language: str | None
    timezone: str | None
    is_private: bool
    created_at: UtcDateTime
    counters: UserCounters
    relationship: Relationship
    presence: Presence | None

"""Модели чтения контекста identity: неизменяемые, формат из спецификации (5.3)."""

import uuid

from pydantic import BaseModel, ConfigDict

from messunjerr.core.me import (
    MeCounters,
    MeExtras,
    MeProfile,
    PrivacySettings,
    RequiredAction,
)
from messunjerr.core.schemas import UtcDateTime
from messunjerr.identity.infra.models import UserRow


class MeUser(BaseModel):
    """Текущий пользователь (5.3): учётная запись, профиль, приватность и счётчики.

    Учётную запись отдаёт identity, остальные разделы приносит порт `MeExtrasProvider`.
    """

    # В примере нет `null`: FastAPI выбрасывает их из схемы OpenAPI, а ключи в ответе всегда есть.
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
                    "username": "ivan",
                    "email": "ivan@example.com",
                    "email_verified": True,
                    "role": "user",
                    "status": "active",
                    "created_at": "2026-10-05T12:34:56.789Z",
                    "profile": {
                        "display_name": "Иван",
                        "avatar": {
                            "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
                            "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
                        },
                        "bio": "Люблю горы",
                        "links": [{"title": "Блог", "url": "https://example.com"}],
                        "birth_date": "1990-05-12",
                        "birth_date_visibility": "hidden",
                        "city": "Казань",
                        "language": "ru",
                        "timezone": "Europe/Moscow",
                        "is_private": False,
                        "hidden_fields": [],
                    },
                    "privacy": {
                        "dm_policy": "friends",
                        "comment_policy": "everyone",
                        "mention_policy": "everyone",
                        "friends_list_visibility": "friends",
                        "followers_list_visibility": "friends",
                        "presence_visibility": "friends",
                        "default_post_visibility": "friends",
                    },
                    "counters": {
                        "unread_notifications": 3,
                        "unread_conversations": 1,
                        "pending_friend_requests": 2,
                        "pending_follow_requests": 0,
                    },
                    "required_actions": [],
                }
            ]
        },
    )

    id: uuid.UUID
    username: str
    email: str
    email_verified: bool
    role: str
    status: str
    created_at: UtcDateTime
    profile: MeProfile
    privacy: PrivacySettings
    counters: MeCounters
    required_actions: list[RequiredAction]

    @classmethod
    def from_row(cls, user: UserRow, extras: MeExtras) -> "MeUser":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            email_verified=user.email_verified_at is not None,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            profile=extras.profile,
            privacy=extras.privacy,
            counters=extras.counters,
            required_actions=extras.required_actions,
        )

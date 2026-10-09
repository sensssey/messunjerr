"""Модели чтения социального графа: неизменяемые, формат из спецификации (5.4)."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from messunjerr.core.schemas import UtcDateTime
from messunjerr.profiles.api_public import Relationship, UserSummary

_ANNA: dict[str, Any] = {
    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
    "username": "anna",
    "display_name": "Анна",
    "avatar": {
        "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
        "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
    },
}


class FriendRequest(BaseModel):
    """Заявка в друзья (5.4): `user` это собеседник, `direction` говорит, чья заявка."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "0192b7a1-0000-7c3a-9d54-3f1a2b6c7d81",
                    "status": "pending",
                    "user": _ANNA,
                    "direction": "outgoing",
                    "created_at": "2026-10-08T12:34:56.789Z",
                }
            ]
        },
    )

    id: uuid.UUID
    status: Literal["pending", "accepted"]
    """В списках всегда `pending`; `accepted` приходит из `POST`, когда встречная заявка принята сразу."""
    user: UserSummary
    direction: Literal["incoming", "outgoing"]
    created_at: UtcDateTime


class AcceptedFriend(BaseModel):
    """Ответ на принятие заявки: новый друг и дата дружбы."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"examples": [{"friend": _ANNA, "since": "2026-10-08T12:40:00.000Z"}]},
    )

    friend: UserSummary
    since: UtcDateTime


class FriendEntry(BaseModel):
    """Строка списка друзей `GET /friends` (5.4)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"examples": [{"user": _ANNA, "since": "2026-10-08T12:40:00.000Z"}]},
    )

    user: UserSummary
    since: UtcDateTime


class BlockEntry(BaseModel):
    """Строка списка блокировок `GET /me/blocks` (5.4)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"examples": [{"user": _ANNA, "blocked_at": "2026-10-08T12:45:00.000Z"}]},
    )

    user: UserSummary
    blocked_at: UtcDateTime


class UserListItem(UserSummary):
    """Человек в чужом списке (5.3): карточка и то, как он связан со зрителем."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    **_ANNA,
                    "relationship": {
                        "is_self": False,
                        "friendship": "friends",
                        "friend_request_id": None,
                        "following": "none",
                        "follows_you": False,
                        "blocked": False,
                    },
                }
            ]
        },
    )

    relationship: Relationship

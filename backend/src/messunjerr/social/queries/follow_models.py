"""Модели чтения подписок: неизменяемые, формат из спецификации (5.4).

Списки подписчиков и подписок отдают уже готовые `UserSummary` (свои списки) и `UserListItem` (чужие,
с полем `relationship`) из `social.queries.models`; здесь только то, чего там нет.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from messunjerr.core.schemas import UtcDateTime
from messunjerr.profiles.api_public import UserSummary

_ANNA: dict[str, Any] = {
    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
    "username": "anna",
    "display_name": "Анна",
    "avatar": {
        "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
        "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
    },
}


class FollowStatus(BaseModel):
    """Ответ `PUT /follows/{user_id}` (5.4): подписка оформлена сразу или создан запрос."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"examples": [{"status": "following"}, {"status": "requested"}]},
    )

    status: Literal["following", "requested"]
    """`following`: подписка есть (профиль открыт или уже была). `requested`: профиль закрыт, запрос ждёт
    ответа владельца."""


class FollowRequest(BaseModel):
    """Входящий запрос на подписку (5.4): `user` это тот, кто просит подписаться."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "0192b7a1-0000-7c3a-9d54-3f1a2b6c7d82",
                    "user": _ANNA,
                    "created_at": "2026-10-09T12:34:56.789Z",
                }
            ]
        },
    )

    id: uuid.UUID
    user: UserSummary
    created_at: UtcDateTime


class ApprovedFollower(BaseModel):
    """Ответ на одобрение запроса (5.4): новый подписчик."""

    model_config = ConfigDict(frozen=True, json_schema_extra={"examples": [{"follower": _ANNA}]})

    follower: UserSummary

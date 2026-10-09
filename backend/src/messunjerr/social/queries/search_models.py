"""Модели чтения поиска людей `GET /search/users` (5.7): неизменяемые, формат из спецификации.

⚖️ Поиск отдаёт только категорию `basic` (ник, отображаемое имя, аватар): карточка `UserSummary` и
то, как человек связан со зрителем (`Relationship`); остальных полей профиля в запрос не попадает.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from messunjerr.profiles.api_public import Relationship, UserSummary

_ANNA: dict[str, Any] = {
    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
    "username": "anna",
    "display_name": "Анна Иванова",
    "avatar": {
        "sm": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/64.webp",
        "md": "/media/public/avatars/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/256.webp",
    },
}
_RELATIONSHIP: dict[str, Any] = {
    "is_self": False,
    "friendship": "none",
    "friend_request_id": None,
    "following": "none",
    "follows_you": False,
    "blocked": False,
}


class UserSearchItem(BaseModel):
    """Найденный человек: карточка и отношение зрителя к нему (5.7)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"examples": [{"user": _ANNA, "relationship": _RELATIONSHIP}]},
    )

    user: UserSummary
    relationship: Relationship


class SearchPage[T](BaseModel):
    """Страница поиска (5.7): смещение вместо курсора, глубже 200 результатов не листают.

    Общая оболочка для людей, постов и тегов; `next_offset` равен `offset + limit`, пока есть ещё
    результаты и потолок не достигнут, иначе `null`.
    """

    items: list[T]
    next_offset: int | None = Field(
        description=(
            "Смещение следующей страницы или `null`, если результаты кончились либо достигнут "
            "потолок в 200. Следующий запрос с этим `offset` берите с `limit ≤ 200 − offset`."
        )
    )


class UserSearchPage(SearchPage[UserSearchItem]):
    """Ответ `GET /search/users`."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {"items": [{"user": _ANNA, "relationship": _RELATIONSHIP}], "next_offset": 20}
            ]
        },
    )

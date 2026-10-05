"""Схемы запросов profiles (5.3): правка профиля и настроек приватности.

`PATCH` работает как JSON Merge Patch (5.1): отсутствующий ключ значит «без изменений», `null` очищает
поле, если это допустимо. Какие ключи прислал клиент, знает `model_fields_set`; `to_changes()` отдаёт
команде только их.
"""

import re
import uuid
from datetime import date
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, StrictBool, field_validator
from pydantic_core import PydanticCustomError

from messunjerr.core.codes import ItemCode
from messunjerr.core.fields import DisplayName, Language, Timezone
from messunjerr.core.limits import LIMITS
from messunjerr.core.me import Audience, BirthDateVisibility, ListVisibility, PostVisibility
from messunjerr.core.schemas import ApiModel
from messunjerr.profiles.domain.rules import is_valid_link_url

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_iso_date(value: Any) -> Any:
    """Принимает только `YYYY-MM-DD`: числа, время и прочие приведения pydantic здесь не нужны."""
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise PydanticCustomError(ItemCode.INVALID_FORMAT.value, "Use the YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PydanticCustomError(
            ItemCode.INVALID_FORMAT.value, "This is not a valid calendar date."
        ) from error


def _valid_url(value: str) -> str:
    if not is_valid_link_url(value):
        raise PydanticCustomError(
            ItemCode.INVALID_FORMAT.value, "Use an absolute http or https URL without credentials."
        )
    return value


IsoDate = Annotated[date, BeforeValidator(_parse_iso_date)]
"""Дата в виде `YYYY-MM-DD`."""


class LinkIn(ApiModel):
    title: Annotated[str, Field(min_length=1, max_length=LIMITS.link_title_max)]
    url: Annotated[
        str, Field(min_length=1, max_length=LIMITS.link_url_max), AfterValidator(_valid_url)
    ]


class UpdateProfileRequest(ApiModel):
    """Все поля необязательны. `null` очищает `bio`, `birth_date`, `city`, `language`, `timezone`,
    `avatar_asset_id` и `links` (пустой список); у `display_name`, `birth_date_visibility` и
    `is_private` `null` недопустим."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "display_name": "Иван",
                    "bio": "Люблю горы",
                    "links": [{"title": "Блог", "url": "https://example.com"}],
                    "birth_date": "1990-05-12",
                    "birth_date_visibility": "day_month",
                    "city": "Казань",
                    "language": "ru",
                    "timezone": "Europe/Moscow",
                    "is_private": False,
                }
            ]
        },
    )

    display_name: DisplayName | None = None
    bio: Annotated[str, Field(max_length=LIMITS.bio_max)] | None = None
    links: Annotated[list[LinkIn], Field(max_length=LIMITS.links_max)] | None = None
    birth_date: IsoDate | None = Field(
        default=None, description="`YYYY-MM-DD`; возраст не меньше `MIN_AGE` (по умолчанию 18)."
    )
    birth_date_visibility: BirthDateVisibility | None = None
    city: Annotated[str, Field(max_length=LIMITS.city_max)] | None = None
    language: Language | None = None
    timezone: Timezone | None = None
    is_private: StrictBool | None = None
    avatar_asset_id: uuid.UUID | None = Field(
        default=None,
        description="Готовый ресурс назначения `avatar` своего владельца; `null` убирает аватар.",
    )

    @field_validator("display_name", "birth_date_visibility", "is_private", mode="after")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        if value is None:
            raise PydanticCustomError(ItemCode.INVALID_FORMAT.value, "This field cannot be null.")
        return value

    def to_changes(self) -> dict[str, Any]:
        """Присланные поля для команды; `links: null` означает пустой список."""
        changes: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if name == "links":
                changes[name] = [link.model_dump() for link in value] if value else []
            else:
                changes[name] = value
        return changes


class UpdatePrivacyRequest(ApiModel):
    """Любые из полей; значения из перечислений 5.3. `null` недопустим."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"dm_policy": "everyone", "presence_visibility": "nobody"}]}
    )

    dm_policy: Audience | None = None
    comment_policy: Audience | None = None
    mention_policy: Audience | None = None
    friends_list_visibility: ListVisibility | None = None
    followers_list_visibility: ListVisibility | None = None
    presence_visibility: Audience | None = None
    default_post_visibility: PostVisibility | None = None

    @field_validator("*", mode="after")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        if value is None:
            raise PydanticCustomError(ItemCode.INVALID_FORMAT.value, "This field cannot be null.")
        return value

    def to_changes(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.model_fields_set}

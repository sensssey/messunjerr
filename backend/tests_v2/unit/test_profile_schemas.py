"""Схемы правки профиля и приватности: merge-patch, нормализация, коды ошибок элементов."""

import uuid
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from messunjerr.profiles.api.schemas import UpdatePrivacyRequest, UpdateProfileRequest


def errors_of(model: type[Any], payload: dict[str, Any]) -> dict[str, str]:
    """Тип ошибки по пути до поля (`links.0.url`)."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return {".".join(str(part) for part in e["loc"]): e["type"] for e in caught.value.errors()}


def profile_errors(payload: dict[str, Any]) -> dict[str, str]:
    return errors_of(UpdateProfileRequest, payload)


# ----------------------------------------------------------------------------- merge-patch
def test_an_empty_body_changes_nothing() -> None:
    assert UpdateProfileRequest.model_validate({}).to_changes() == {}
    assert UpdatePrivacyRequest.model_validate({}).to_changes() == {}


def test_only_sent_keys_reach_the_command() -> None:
    request = UpdateProfileRequest.model_validate({"display_name": "Иван", "bio": None})
    assert request.to_changes() == {"display_name": "Иван", "bio": None}


def test_null_clears_the_fields_that_may_be_cleared() -> None:
    nullable = {
        "bio": None,
        "birth_date": None,
        "city": None,
        "language": None,
        "timezone": None,
        "avatar_asset_id": None,
    }
    assert UpdateProfileRequest.model_validate(nullable).to_changes() == nullable


def test_null_links_mean_an_empty_list() -> None:
    assert UpdateProfileRequest.model_validate({"links": None}).to_changes() == {"links": []}
    assert UpdateProfileRequest.model_validate({"links": []}).to_changes() == {"links": []}


@pytest.mark.parametrize("field", ["display_name", "birth_date_visibility", "is_private"])
def test_null_is_not_allowed_where_a_value_is_required(field: str) -> None:
    assert profile_errors({field: None}) == {field: "invalid_format"}


def test_values_are_parsed_into_their_types() -> None:
    asset = uuid.uuid4()
    changes = UpdateProfileRequest.model_validate(
        {"birth_date": "1990-05-12", "avatar_asset_id": str(asset), "is_private": True}
    ).to_changes()
    assert changes == {
        "birth_date": date(1990, 5, 12),
        "avatar_asset_id": asset,
        "is_private": True,
    }


def test_links_become_plain_dictionaries_for_storage() -> None:
    changes = UpdateProfileRequest.model_validate(
        {"links": [{"title": " Блог ", "url": "https://example.com/a"}]}
    ).to_changes()
    assert changes == {"links": [{"title": "Блог", "url": "https://example.com/a"}]}


# ----------------------------------------------------------------------------- строки
def test_strings_are_trimmed_and_normalized() -> None:
    request = UpdateProfileRequest.model_validate(
        {"display_name": "  Zoé  ", "bio": "  строка\nвторая  ", "city": " Казань "}
    )
    assert request.display_name == "Zoé"  # NFC: «e» и знак ударения склеиваются в «é»
    assert request.bio == "строка\nвторая"
    assert request.city == "Казань"


@pytest.mark.parametrize("field", ["display_name", "bio", "city"])
def test_control_characters_are_rejected(field: str) -> None:
    assert profile_errors({field: "a\u0000b"}) == {field: "invalid_format"}
    assert profile_errors({field: "a\u0007b"}) == {field: "invalid_format"}


def test_length_limits() -> None:
    assert profile_errors({"display_name": ""}) == {"display_name": "string_too_short"}
    assert profile_errors({"display_name": "   "}) == {"display_name": "string_too_short"}
    assert profile_errors({"display_name": "я" * 51}) == {"display_name": "string_too_long"}
    assert UpdateProfileRequest.model_validate({"display_name": "я" * 50}).display_name
    assert profile_errors({"bio": "я" * 501}) == {"bio": "string_too_long"}
    assert UpdateProfileRequest.model_validate({"bio": "я" * 500}).bio
    assert profile_errors({"city": "я" * 101}) == {"city": "string_too_long"}
    assert UpdateProfileRequest.model_validate({"bio": ""}).to_changes() == {"bio": ""}


# ----------------------------------------------------------------------------- ссылки
def test_at_most_five_links() -> None:
    link = {"title": "t", "url": "https://example.com"}
    assert UpdateProfileRequest.model_validate({"links": [link] * 5}).links
    assert profile_errors({"links": [link] * 6}) == {"links": "too_long"}


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ({"title": "t", "url": "javascript:alert(1)"}, {"links.0.url": "invalid_format"}),
        ({"title": "t", "url": "example.com"}, {"links.0.url": "invalid_format"}),
        ({"title": "t", "url": "https://u:p@example.com"}, {"links.0.url": "invalid_format"}),
        ({"title": "", "url": "https://example.com"}, {"links.0.title": "string_too_short"}),
        ({"title": "т" * 41, "url": "https://example.com"}, {"links.0.title": "string_too_long"}),
        ({"title": "t", "url": "https://e.com/" + "a" * 300}, {"links.0.url": "string_too_long"}),
        ({"title": "t"}, {"links.0.url": "missing"}),
        ({"url": "https://example.com"}, {"links.0.title": "missing"}),
        (
            {"title": "t", "url": "https://example.com", "rel": "me"},
            {"links.0.rel": "extra_forbidden"},
        ),
    ],
)
def test_link_errors_point_into_the_list(link: dict[str, Any], expected: dict[str, str]) -> None:
    assert profile_errors({"links": [link]}) == expected


# ----------------------------------------------------------------------------- дата рождения
@pytest.mark.parametrize(
    "value",
    ["12.05.1990", "1990-5-12", "1990-13-01", "1990-02-30", "19900512", 19900512, "", "yesterday"],
)
def test_birth_date_only_accepts_iso_dates(value: Any) -> None:
    assert profile_errors({"birth_date": value}) == {"birth_date": "invalid_format"}


def test_birth_date_does_not_accept_timestamps_or_datetimes() -> None:
    assert profile_errors({"birth_date": "1990-05-12T00:00:00"}) == {"birth_date": "invalid_format"}
    assert profile_errors({"birth_date": 1_000_000_000}) == {"birth_date": "invalid_format"}


# ----------------------------------------------------------------------------- перечисления и типы
def test_enumerations_report_invalid_enum() -> None:
    assert profile_errors({"birth_date_visibility": "everyone"}) == {
        "birth_date_visibility": "literal_error"
    }
    assert errors_of(UpdatePrivacyRequest, {"dm_policy": "only_me"}) == {
        "dm_policy": "literal_error"
    }
    assert errors_of(UpdatePrivacyRequest, {"friends_list_visibility": "nobody"}) == {
        "friends_list_visibility": "literal_error"
    }
    assert errors_of(UpdatePrivacyRequest, {"default_post_visibility": "everyone"}) == {
        "default_post_visibility": "literal_error"
    }


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, "1"])
def test_is_private_must_be_a_real_boolean(value: Any) -> None:
    assert profile_errors({"is_private": value}) == {"is_private": "bool_type"}


@pytest.mark.parametrize("value", ["", "abc", "123", 5, "0192b7a0-5c1e-7c3a-9d54"])
def test_avatar_asset_id_must_be_a_uuid(value: Any) -> None:
    errors = profile_errors({"avatar_asset_id": value})
    assert list(errors) == ["avatar_asset_id"]


def test_unknown_fields_are_rejected() -> None:
    assert profile_errors({"username": "x"}) == {"username": "extra_forbidden"}
    assert profile_errors({"role": "admin"}) == {"role": "extra_forbidden"}
    assert errors_of(UpdatePrivacyRequest, {"is_private": True}) == {
        "is_private": "extra_forbidden"
    }


def test_language_and_timezone_use_the_shared_rules() -> None:
    request = UpdateProfileRequest.model_validate({"language": "en-us", "timezone": "Asia/Tokyo"})
    assert (request.language, request.timezone) == ("en-US", "Asia/Tokyo")
    assert profile_errors({"language": "eng-USA-x"}) == {"language": "invalid_format"}
    assert profile_errors({"timezone": "Tokyo"}) == {"timezone": "invalid_format"}


def test_all_problems_are_reported_together() -> None:
    assert profile_errors(
        {"display_name": "", "birth_date": "nope", "language": "?", "is_private": "x"}
    ) == {
        "display_name": "string_too_short",
        "birth_date": "invalid_format",
        "language": "invalid_format",
        "is_private": "bool_type",
    }


# ----------------------------------------------------------------------------- приватность
def test_privacy_accepts_any_subset() -> None:
    request = UpdatePrivacyRequest.model_validate(
        {"dm_policy": "everyone", "presence_visibility": "nobody"}
    )
    assert request.to_changes() == {"dm_policy": "everyone", "presence_visibility": "nobody"}


@pytest.mark.parametrize(
    "field",
    [
        "dm_policy",
        "comment_policy",
        "mention_policy",
        "friends_list_visibility",
        "followers_list_visibility",
        "presence_visibility",
        "default_post_visibility",
    ],
)
def test_privacy_values_cannot_be_null(field: str) -> None:
    assert errors_of(UpdatePrivacyRequest, {field: None}) == {field: "invalid_format"}


def test_privacy_covers_exactly_the_stored_settings() -> None:
    from messunjerr.profiles.commands.update_privacy import PRIVACY_FIELDS

    assert set(UpdatePrivacyRequest.model_fields) == PRIVACY_FIELDS


def test_profile_covers_exactly_the_patchable_columns() -> None:
    from messunjerr.profiles.commands.update_profile import PROFILE_FIELDS

    assert set(UpdateProfileRequest.model_fields) == PROFILE_FIELDS

"""Схемы запросов identity: нормализация, границы, коды ошибок элементов."""

from typing import Any

import pytest
from pydantic import ValidationError

from messunjerr.identity.api.schemas import (
    LoginRequest,
    RegisterRequest,
    ResendVerificationRequest,
    VerifyEmailRequest,
)

VALID: dict[str, Any] = {
    "email": "user@example.com",
    "username": "ivan",
    "password": "correct horse battery staple",
    "accept_terms": True,
}


def error_types(payload: dict[str, Any], model: type[Any] = RegisterRequest) -> dict[str, str]:
    """Тип ошибки по имени поля."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return {str(error["loc"][0]): error["type"] for error in caught.value.errors()}


def test_valid_registration_is_normalized() -> None:
    request = RegisterRequest.model_validate(
        {**VALID, "email": "  User@Example.COM ", "username": "  Ivan_77 "}
    )
    assert request.email == "user@example.com"
    assert request.username == "ivan_77"
    assert request.accept_terms is True


def test_password_is_kept_exactly_as_typed() -> None:
    request = RegisterRequest.model_validate({**VALID, "password": "  spaces kept 12  "})
    assert request.password == "  spaces kept 12  "


def test_unknown_fields_are_rejected_including_profile_fields_of_the_final_contract() -> None:
    assert error_types({**VALID, "display_name": "Иван"}) == {"display_name": "extra_forbidden"}
    assert error_types({**VALID, "language": "ru"}) == {"language": "extra_forbidden"}


def test_the_schema_leaves_the_consent_decision_to_the_command() -> None:
    """Отсутствие или `false` схема пропускает: `consent_missing` выдаёт команда регистрации."""
    without = {key: value for key, value in VALID.items() if key != "accept_terms"}
    assert RegisterRequest.model_validate(without).accept_terms is False
    assert RegisterRequest.model_validate({**VALID, "accept_terms": False}).accept_terms is False


@pytest.mark.parametrize("value", ["true", "yes", 1, "1", None])
def test_terms_checkbox_must_be_a_real_boolean(value: Any) -> None:
    assert error_types({**VALID, "accept_terms": value}) == {"accept_terms": "bool_type"}


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("ab", "string_too_short"),
        ("x" * 31, "string_too_long"),
        ("bad name", "string_pattern_mismatch"),
        ("иван", "string_pattern_mismatch"),
        ("a-b", "string_pattern_mismatch"),
    ],
)
def test_username_constraints(username: str, expected: str) -> None:
    assert error_types({**VALID, "username": username}) == {"username": expected}


@pytest.mark.parametrize(
    ("password", "expected"),
    [("x" * 9, "string_too_short"), ("x" * 129, "string_too_long")],
)
def test_password_bounds(password: str, expected: str) -> None:
    assert error_types({**VALID, "password": password}) == {"password": expected}


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("nope", "invalid_format"),
        ("a@", "invalid_format"),
        ("a" * 250 + "@example.com", "string_too_long"),
    ],
)
def test_email_errors_use_catalog_codes(email: str, expected: str) -> None:
    assert error_types({**VALID, "email": email}) == {"email": expected}


def test_every_format_error_is_reported_at_once() -> None:
    assert error_types({"email": "x", "username": "a", "password": "p"}) == {
        "email": "invalid_format",
        "username": "string_too_short",
        "password": "string_too_short",
    }


def test_login_request() -> None:
    request = LoginRequest.model_validate(
        {"login": "  Ivan  ", "password": " pass ", "device_label": " Firefox "}
    )
    assert request.login == "Ivan"
    assert request.password == " pass "
    assert request.device_label == "Firefox"
    assert error_types({"login": "x", "password": "y" * 129}, LoginRequest) == {
        "password": "string_too_long"
    }
    assert error_types(
        {"login": "x", "password": "y", "device_label": "z" * 101}, LoginRequest
    ) == {"device_label": "string_too_long"}


def test_email_only_requests() -> None:
    assert (
        ResendVerificationRequest.model_validate({"email": " A@Example.com "}).email
        == "a@example.com"
    )
    assert VerifyEmailRequest.model_validate({"token": " abc "}).token == "abc"
    assert error_types({"token": ""}, VerifyEmailRequest) == {"token": "string_too_short"}

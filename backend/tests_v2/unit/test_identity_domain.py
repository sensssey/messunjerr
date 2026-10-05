"""Доменные правила identity без БД: ники, почта, пароли, непрозрачные токены."""

import hashlib
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from messunjerr.core.security import hash_token, new_opaque_token
from messunjerr.identity.domain.emails import EmailProblem, InvalidEmailError, normalize_email
from messunjerr.identity.domain.passwords import (
    PasswordProblem,
    check_password_policy,
    common_passwords,
)
from messunjerr.identity.domain.usernames import (
    RESERVED_USERNAMES,
    USERNAME_PATTERN,
    UsernameProblem,
    check_username,
    normalize_username,
)


# ----------------------------------------------------------------------------- ники
@pytest.mark.parametrize(("raw", "expected"), [("Ivan", "ivan"), ("  Anna_1  ", "anna_1")])
def test_username_is_stored_lowercase_without_edge_spaces(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("name", ["ivan", "a_b", "abc", "x" * 30, "user_123", "007"])
def test_valid_usernames(name: str) -> None:
    assert check_username(name) is None


@pytest.mark.parametrize(
    "name", ["ab", "x" * 31, "иван", "a-b", "a b", "a.b", "", "UPPER", "a@b", "ivan\n"]
)
def test_invalid_usernames(name: str) -> None:
    assert check_username(name) is UsernameProblem.INVALID


@pytest.mark.parametrize("name", ["admin", "root", "support", "messunjerr", "api", "www"])
def test_reserved_usernames(name: str) -> None:
    assert check_username(name) is UsernameProblem.RESERVED


def test_every_reserved_name_could_otherwise_be_registered() -> None:
    """Запись в списке имеет смысл только для ников, прошедших бы проверку формата."""
    assert all(USERNAME_PATTERN.fullmatch(name) for name in RESERVED_USERNAMES)


# ----------------------------------------------------------------------------- почта
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("User@Example.COM", "user@example.com"),
        ("  a.b+tag@example.org ", "a.b+tag@example.org"),
        ("тест@пример.рф", "тест@пример.рф"),
    ],
)
def test_email_is_stored_lowercase(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain",
        "a@",
        "@example.com",
        "a@@example.com",
        "a b@example.com",
        "a@localhost",
        "a@mail.test",
    ],
)
def test_invalid_email(raw: str) -> None:
    with pytest.raises(InvalidEmailError) as caught:
        normalize_email(raw)
    assert caught.value.problem is EmailProblem.INVALID


def test_too_long_email() -> None:
    with pytest.raises(InvalidEmailError) as caught:
        normalize_email("a" * 250 + "@example.com")
    assert caught.value.problem is EmailProblem.TOO_LONG


# ----------------------------------------------------------------------------- пароли
def test_common_password_list_is_loaded_and_normalized() -> None:
    words = common_passwords()
    assert len(words) > 5000
    assert "1234567890" in words
    assert all(len(word) >= 10 and word == word.casefold() for word in words)
    assert not any(word.startswith("#") for word in words)


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", PasswordProblem.TOO_SHORT),
        ("x" * 9, PasswordProblem.TOO_SHORT),
        ("a1b2c3d4e5" * 13, PasswordProblem.TOO_LONG),
        ("qwertyuiop", PasswordProblem.TOO_COMMON),
        ("QWERTYUIOP", PasswordProblem.TOO_COMMON),
        ("1234567890", PasswordProblem.TOO_COMMON),
        ("kkkkkkkkkkkkk", PasswordProblem.TOO_SIMPLE),
        ("0505050505050", PasswordProblem.TOO_SIMPLE),
        ("qzqzqzqzqzqz", PasswordProblem.TOO_SIMPLE),
    ],
)
def test_weak_passwords(password: str, problem: PasswordProblem) -> None:
    assert check_password_policy(password) is problem


def test_password_equal_to_username_or_email_is_rejected() -> None:
    assert (
        check_password_policy("IvanPetrov77", username="ivanpetrov77")
        is PasswordProblem.SAME_AS_USERNAME
    )
    assert (
        check_password_policy("ivan.petrov@example.com", email="Ivan.Petrov@Example.com")
        is PasswordProblem.SAME_AS_EMAIL
    )
    assert (
        check_password_policy("ivan.petrov.long", email="ivan.petrov.long@example.com")
        is PasswordProblem.SAME_AS_EMAIL
    )


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",
        "Tr0ub4dor&3-horse",
        "пароль-с-кириллицей-123",
        " " * 4 + "ab1 cd2 ef3",
    ],
)
def test_reasonable_passwords_pass(password: str) -> None:
    assert check_password_policy(password, username="ivan", email="ivan@example.com") is None


@settings(deadline=None, max_examples=200)
@given(st.text(min_size=0, max_size=200))
def test_policy_never_raises_and_respects_length_bounds(password: str) -> None:
    result = check_password_policy(password, username="user", email="user@example.com")
    if len(password) < 10:
        assert result is PasswordProblem.TOO_SHORT
    elif len(password) > 128:
        assert result is PasswordProblem.TOO_LONG
    else:
        assert result is None or isinstance(result, PasswordProblem)


# ----------------------------------------------------------------------------- токены
def test_opaque_token_has_256_bits_and_is_url_safe() -> None:
    token = new_opaque_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)


def test_opaque_tokens_are_unique() -> None:
    assert len({new_opaque_token() for _ in range(2000)}) == 2000


def test_token_hash_is_sha256_digest() -> None:
    digest = hash_token("abc")
    assert digest == hashlib.sha256(b"abc").digest()
    assert len(digest) == 32

"""`{ref}` из `/users/{ref}`: UUID или ник, всё остальное это «такого нет» (5.3)."""

import uuid

import pytest

from messunjerr.identity.domain.usernames import parse_user_ref

ID = uuid.UUID("0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80")


def test_a_canonical_uuid_is_an_id() -> None:
    assert parse_user_ref(str(ID)) == ID
    assert parse_user_ref(str(ID).upper()) == ID


@pytest.mark.parametrize(
    ("ref", "username"),
    [("ivan", "ivan"), ("Ivan_77", "ivan_77"), ("  ivan  ", "ivan"), ("a" * 30, "a" * 30)],
)
def test_a_username_is_lowercased(ref: str, username: str) -> None:
    assert parse_user_ref(ref) == username


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "ab",
        "a" * 31,
        "ivan-petrov",
        "ivan petrov",
        "иван",
        "0192b7a05c1e7c3a9d543f1a2b6c7d80",  # UUID без дефисов не принимаем: форма одна
        "{0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80}",
        "urn:uuid:0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
        "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d8",
        "../etc/passwd",
        "ivan%00",
    ],
)
def test_anything_else_is_not_a_ref(ref: str) -> None:
    assert parse_user_ref(ref) is None


def test_a_username_never_looks_like_a_uuid() -> None:
    """Ник не содержит дефисов, поэтому разбор однозначен (5.3)."""
    assert isinstance(parse_user_ref("a" * 8 + "_" + "b" * 4), str)
    assert parse_user_ref("a" * 8 + "-" + "b" * 4) is None

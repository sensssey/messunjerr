"""Политика паролей (4.7): 10–128 символов, не из списка частых паролей, не совпадает с ником и почтой.

Чистые функции. Список частых паролей лежит в `data/common_passwords.txt` (как он собран, описано
в scripts/build_common_passwords.py) и читается один раз.
"""

import unicodedata
from enum import StrEnum
from functools import cache
from importlib import resources

from messunjerr.core.limits import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH

# Пароль из трёх и менее разных символов ("aaaaaaaaaa", "1212121212") слаб при любой длине.
_MIN_DISTINCT_CHARACTERS = 4


class PasswordProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_COMMON = "too_common"
    TOO_SIMPLE = "too_simple"
    SAME_AS_USERNAME = "same_as_username"
    SAME_AS_EMAIL = "same_as_email"


@cache
def common_passwords() -> frozenset[str]:
    data = resources.files("messunjerr.identity.domain").joinpath("data", "common_passwords.txt")
    lines = (line.strip() for line in data.read_text(encoding="utf-8").splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def check_password_policy(
    password: str, *, username: str | None = None, email: str | None = None
) -> PasswordProblem | None:
    """Первая найденная проблема или `None`. Сравнения без учёта регистра и в форме NFC."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return PasswordProblem.TOO_SHORT
    if len(password) > PASSWORD_MAX_LENGTH:
        return PasswordProblem.TOO_LONG

    candidate = unicodedata.normalize("NFC", password).casefold()
    if candidate in common_passwords():
        return PasswordProblem.TOO_COMMON
    if len(set(candidate)) < _MIN_DISTINCT_CHARACTERS:
        return PasswordProblem.TOO_SIMPLE
    if username and candidate == username.casefold():
        return PasswordProblem.SAME_AS_USERNAME
    if email:
        address = email.casefold()
        if candidate in (address, address.partition("@")[0]):
            return PasswordProblem.SAME_AS_EMAIL
    return None

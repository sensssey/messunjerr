"""Нормализация и проверка адреса электронной почты (4.5: хранится в нижнем регистре, ≤ 254)."""

from enum import StrEnum

from email_validator import EmailNotValidError, validate_email

EMAIL_MAX_LENGTH = 254


class EmailProblem(StrEnum):
    TOO_LONG = "too_long"
    INVALID = "invalid"


class InvalidEmailError(ValueError):
    def __init__(self, problem: EmailProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


def mask_email(address: str) -> str:
    """`ivan.petrov@example.com` -> `i***@example.com`: для писем, где адрес нельзя показывать целиком."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def normalize_email(raw: str) -> str:
    """Возвращает адрес в виде хранения или бросает `InvalidEmailError`.

    Доставляемость (DNS) не проверяется: это решает отправка письма. Служебные домены (`.test`,
    `.local`, `.invalid`) библиотека отвергает; `example.com` допустим.
    """
    value = raw.strip()
    if len(value) > EMAIL_MAX_LENGTH:
        raise InvalidEmailError(EmailProblem.TOO_LONG)
    try:
        checked = validate_email(value, check_deliverability=False)
    except EmailNotValidError as error:
        raise InvalidEmailError(EmailProblem.INVALID) from error
    return checked.normalized.lower()

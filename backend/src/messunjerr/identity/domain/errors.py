"""Фабрики ошибок контекста identity: код и формулировка из каталога 5.14 в одном месте."""

from datetime import datetime
from typing import Any

from messunjerr.core.codes import ErrorCode, ItemCode
from messunjerr.core.errors import DomainError, ErrorItem
from messunjerr.core.schemas import format_utc

_BEARER = "Bearer"


def field_error(pointer: str, code: ItemCode, detail: str, **meta: Any) -> DomainError:
    """`422 validation_error` с одним элементом `errors[]`."""
    return DomainError(
        ErrorCode.VALIDATION_ERROR,
        errors=[ErrorItem(pointer=pointer, code=code, detail=detail, meta=meta)],
    )


def username_taken() -> DomainError:
    return DomainError(ErrorCode.USERNAME_TAKEN, "This username is already taken.")


def invalid_credentials() -> DomainError:
    return DomainError(ErrorCode.INVALID_CREDENTIALS, "The login or password is incorrect.")


def email_not_verified() -> DomainError:
    return DomainError(ErrorCode.EMAIL_NOT_VERIFIED, "Confirm your email address to sign in.")


def account_suspended(until: datetime | None) -> DomainError:
    extensions: dict[str, Any] = {"suspended_until": format_utc(until) if until else None}
    return DomainError(ErrorCode.ACCOUNT_SUSPENDED, "The account is suspended.", **extensions)


def account_banned() -> DomainError:
    return DomainError(ErrorCode.ACCOUNT_BANNED, "The account is banned.")


def token_invalid_or_expired() -> DomainError:
    return DomainError(
        ErrorCode.TOKEN_INVALID_OR_EXPIRED, "The token is invalid, expired or already used."
    )


def refresh_missing() -> DomainError:
    return DomainError(ErrorCode.REFRESH_MISSING, "The refresh cookie is missing.")


def refresh_invalid() -> DomainError:
    return DomainError(ErrorCode.REFRESH_INVALID, "The refresh token is unknown or revoked.")


def refresh_expired() -> DomainError:
    return DomainError(ErrorCode.REFRESH_EXPIRED, "The refresh token has expired.")


def refresh_reused() -> DomainError:
    return DomainError(
        ErrorCode.REFRESH_REUSED,
        "The refresh token was used again after rotation; the session has been revoked.",
    )


def reauth_failed() -> DomainError:
    return DomainError(ErrorCode.REAUTH_FAILED, "The password is incorrect.")


def service_unavailable(detail: str) -> DomainError:
    return DomainError(ErrorCode.SERVICE_UNAVAILABLE, detail, headers={"Retry-After": "5"})


def not_found(
    detail: str = "The resource does not exist or is not available to you.",
) -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, detail)


def unauthorized(code: ErrorCode, detail: str) -> DomainError:
    """`401` с заголовком `WWW-Authenticate` (RFC 6750): без токена только схема, иначе ошибка."""
    challenge = _BEARER if code is ErrorCode.TOKEN_MISSING else f'{_BEARER} error="invalid_token"'
    return DomainError(code, detail, headers={"WWW-Authenticate": challenge})

"""Исключения предметной области: обработчики превращают их в ответы problem+json (RFC 9457)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from messunjerr.core.codes import PROBLEM_SPECS, ErrorCode, ItemCode


@dataclass(frozen=True, slots=True)
class ErrorItem:
    """Один элемент `errors[]` у `validation_error`: что именно не так и где."""

    pointer: str
    code: ItemCode | str
    detail: str
    meta: dict[str, Any] = field(default_factory=dict[str, Any])


class DomainError(Exception):
    """Ошибка с кодом из каталога 5.14. Статус и название берутся из каталога по коду."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        *,
        errors: Sequence[ErrorItem] = (),
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.status, self.title = PROBLEM_SPECS[code]
        self.detail = detail
        self.errors = tuple(errors)
        self.headers = dict(headers or {})
        self.extensions = extensions


class NotFoundError(DomainError):
    """Ресурса нет или он не виден зрителю: наружу это одно и то же (4.6)."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(ErrorCode.NOT_FOUND, detail)


class InvalidCursorError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.INVALID_CURSOR, "The cursor is malformed or does not belong to this list."
        )


class ValidationFailedError(DomainError):
    def __init__(self, errors: Sequence[ErrorItem], detail: str | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, detail, errors=errors)


class RateLimitedError(DomainError):
    def __init__(self, retry_after: int, detail: str | None = None) -> None:
        super().__init__(
            ErrorCode.RATE_LIMITED,
            detail,
            headers={"Retry-After": str(retry_after)},
            retry_after=retry_after,
        )


class ServiceUnavailableError(DomainError):
    def __init__(self, detail: str | None = None, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE, detail, headers=headers)

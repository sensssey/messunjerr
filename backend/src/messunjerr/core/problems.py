"""Ответы problem+json (RFC 9457) и обработчики исключений.

Формат из 5.1: `type`, `title`, `status`, `code`, `detail`, `instance`, `request_id` и, для
`validation_error`, список `errors[]`. Значения входных полей в ответ **не** подставляются.
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import Scope

from messunjerr.core.codes import PROBLEM_SPECS, ErrorCode, ItemCode
from messunjerr.core.errors import DomainError, ErrorItem
from messunjerr.core.ids import uuid7
from messunjerr.core.logs import get_logger

PROBLEM_MEDIA_TYPE = "application/problem+json"

_DEFAULT_DETAIL: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "The requested resource does not exist or is not available to you.",
    ErrorCode.METHOD_NOT_ALLOWED: "This method is not supported for the resource.",
    ErrorCode.VALIDATION_ERROR: "One or more fields are invalid.",
    ErrorCode.INVALID_REQUEST: "The request could not be understood.",
    ErrorCode.INTERNAL_ERROR: "Unexpected error. Quote the request_id when contacting support.",
    ErrorCode.PAYLOAD_TOO_LARGE: "The request body is too large.",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "The request body must be application/json.",
    ErrorCode.RATE_LIMITED: "Too many requests. Try again later.",
    ErrorCode.SERVICE_UNAVAILABLE: "A required dependency is temporarily unavailable.",
}

# Тип ошибки pydantic -> код элемента из каталога 5.14. Всё неизвестное считается `invalid_format`.
_PYDANTIC_TO_ITEM: dict[str, ItemCode] = {
    "missing": ItemCode.REQUIRED,
    "string_too_short": ItemCode.STRING_TOO_SHORT,
    "string_too_long": ItemCode.STRING_TOO_LONG,
    "too_short": ItemCode.OUT_OF_RANGE,
    "too_long": ItemCode.TOO_MANY_ITEMS,
    "extra_forbidden": ItemCode.UNKNOWN_FIELD,
    "enum": ItemCode.INVALID_ENUM,
    "literal_error": ItemCode.INVALID_ENUM,
    "greater_than": ItemCode.OUT_OF_RANGE,
    "greater_than_equal": ItemCode.OUT_OF_RANGE,
    "less_than": ItemCode.OUT_OF_RANGE,
    "less_than_equal": ItemCode.OUT_OF_RANGE,
    "multiple_of": ItemCode.OUT_OF_RANGE,
}
_KNOWN_ITEM_CODES = {code.value for code in ItemCode}

# Состояния PostgreSQL «подождите и повторите»: не дождались замка (`lock_timeout`, 5 с у роли `app`),
# взаимная блокировка, сбой сериализации, запрос снят по `statement_timeout`. Это не поломка сервера,
# а затор на общих строках (например, долгое открытие профиля с тысячами запросов), поэтому ответ
# `503` с `Retry-After`, а не `500`.
CONTENTION_SQLSTATES = frozenset({"55P03", "40P01", "40001", "57014"})
CONTENTION_RETRY_SECONDS = 1


class ProblemItem(BaseModel):
    pointer: str = Field(examples=["/body/body"])
    code: str = Field(examples=["string_too_long"])
    detail: str
    meta: dict[str, Any] = Field(default_factory=dict)


class Problem(BaseModel):
    """Схема ответа об ошибке для OpenAPI."""

    type: str = Field(examples=["/problems/validation_error"])
    title: str
    status: int
    code: str
    detail: str
    instance: str
    request_id: str
    errors: list[ProblemItem] | None = None


def request_id_of(scope: Scope) -> str:
    """Идентификатор запроса: из scope (его кладёт middleware), иначе из логов, иначе новый."""
    state = scope.get("state")
    if isinstance(state, dict):
        value = state.get("request_id")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(value, str):
            return value
    bound = structlog.contextvars.get_contextvars().get("request_id")
    return bound if isinstance(bound, str) else uuid7().hex


def build_problem(
    code: ErrorCode,
    *,
    instance: str,
    request_id: str,
    detail: str | None = None,
    errors: Sequence[ErrorItem] = (),
    extensions: Mapping[str, Any] | None = None,
    status: int | None = None,
) -> dict[str, Any]:
    spec_status, title = PROBLEM_SPECS[code]
    body: dict[str, Any] = {
        "type": f"/problems/{code.value}",
        "title": title,
        "status": status or spec_status,
        "code": code.value,
        "detail": detail or _DEFAULT_DETAIL.get(code, title),
        "instance": instance,
        "request_id": request_id,
    }
    if errors:
        body["errors"] = [
            {"pointer": e.pointer, "code": str(e.code), "detail": e.detail, "meta": e.meta}
            for e in errors
        ]
    if extensions:
        body.update(extensions)
    return body


def problem_response(
    code: ErrorCode,
    scope: Scope,
    *,
    detail: str | None = None,
    errors: Sequence[ErrorItem] = (),
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
    status: int | None = None,
) -> JSONResponse:
    request_id = request_id_of(scope)
    body = build_problem(
        code,
        instance=str(scope.get("path", "")),
        request_id=request_id,
        detail=detail,
        errors=errors,
        extensions=extensions,
        status=status,
    )
    # Ответ об ошибке не кэшируется нигде: в нём может быть и состояние аккаунта, и `request_id`.
    # `RateLimit-*` берутся из состояния запроса (их кладёт проверка лимитов), явные заголовки главнее.
    state = scope.get("state")
    limit_headers: dict[str, str] = {}
    if isinstance(state, dict):
        stashed = state.get("ratelimit_headers")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(stashed, dict):
            limit_headers = cast("dict[str, str]", stashed)
    response_headers = {
        "X-Request-ID": request_id,
        "Cache-Control": "no-store",
        **limit_headers,
        **(headers or {}),
    }
    return JSONResponse(
        body,
        status_code=body["status"],
        headers=response_headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _pointer(loc: Sequence[Any]) -> str:
    parts = (str(part).replace("~", "~0").replace("/", "~1") for part in loc)
    return "/" + "/".join(parts)


def _meta(ctx: Any) -> dict[str, Any]:
    if not isinstance(ctx, dict):
        return {}
    context = cast("dict[Any, Any]", ctx)
    return {
        str(key): value
        for key, value in context.items()
        if key != "error" and isinstance(value, str | int | float | bool)
    }


def validation_items(errors: Sequence[Mapping[str, Any]]) -> list[ErrorItem]:
    """Переводит ошибки pydantic в элементы каталога. Поле `input` не используется нигде."""
    items: list[ErrorItem] = []
    for error in errors:
        kind = str(error.get("type", ""))
        code: ItemCode | str
        if kind in _KNOWN_ITEM_CODES:
            code = kind
        else:
            code = _PYDANTIC_TO_ITEM.get(kind, ItemCode.INVALID_FORMAT)
        items.append(
            ErrorItem(
                pointer=_pointer(error.get("loc", ())),
                code=code,
                detail=str(error.get("msg", "Invalid value.")),
                meta=_meta(error.get("ctx")),
            )
        )
    return items


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def install_problem_handlers(app: FastAPI) -> None:
    log = get_logger("messunjerr.errors")

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return problem_response(
            exc.code,
            request.scope,
            detail=exc.detail,
            errors=exc.errors,
            headers=exc.headers,
            extensions=exc.extensions,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        if any(error.get("type") == "json_invalid" for error in errors):
            return problem_response(
                ErrorCode.INVALID_REQUEST,
                request.scope,
                detail="The request body is not valid JSON.",
            )
        return problem_response(
            ErrorCode.VALIDATION_ERROR, request.scope, errors=validation_items(errors)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code)
        if code is None:
            code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.INVALID_REQUEST
            detail = None
            status: int | None = exc.status_code
        else:
            detail = None
            status = None
        headers = {k: v for k, v in (exc.headers or {}).items() if k.lower() == "allow"}
        return problem_response(code, request.scope, detail=detail, headers=headers, status=status)

    @app.exception_handler(DBAPIError)
    async def _database(request: Request, exc: DBAPIError) -> JSONResponse:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate not in CONTENTION_SQLSTATES:
            raise exc  # прочие ошибки БД остаются неожиданными: их ловит обработчик ниже, как и раньше
        # Без `exc_info`: в тексте ошибки SQL, а это лишний шум; нужны только путь и состояние.
        log.warning("database_contention", path=request.url.path, sqlstate=sqlstate)
        return problem_response(
            ErrorCode.SERVICE_UNAVAILABLE,
            request.scope,
            headers={"Retry-After": str(CONTENTION_RETRY_SECONDS)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", path=request.url.path, exc_info=exc)
        return problem_response(ErrorCode.INTERNAL_ERROR, request.scope)

"""ASGI-middleware: контекст запроса (request_id, access-лог) и защита входа (размер и тип тела).

Написаны как чистый ASGI, а не через `BaseHTTPMiddleware`: тот буферизует ответы и ломает потоки
(SSE появится в S10).
"""

import re
import time
from collections.abc import Iterable
from typing import Any

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from messunjerr.core.codes import ErrorCode
from messunjerr.core.ids import uuid7
from messunjerr.core.logs import get_logger
from messunjerr.core.problems import problem_response

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_QUIET_PATH_PREFIXES = ("/health/",)


class RequestContextMiddleware:
    """Присваивает запросу `request_id`, возвращает его в заголовке и пишет access-лог.

    В лог попадают метод, путь (без query: в нём может быть `ticket`), шаблон маршрута, статус и
    длительность. IP клиента и заголовки в логе приложения не пишутся (см. 4.15).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._log = get_logger("messunjerr.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _REQUEST_ID.match(incoming) else uuid7().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = str(scope.get("path", ""))[:200]
            route: Any = scope.get("route")
            level = "error" if status_code >= 500 else "info"
            if path.startswith(_QUIET_PATH_PREFIXES) and status_code < 500:
                level = "debug"
            getattr(self._log, level)(
                "http_request",
                method=scope.get("method"),
                path=path,
                route=getattr(route, "path", None),
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()


class _PayloadTooLargeError(Exception):
    pass


class RequestGuardMiddleware:
    """Отсекает слишком большие тела (413) и не-JSON там, где ждут JSON (415).

    Файлы идут напрямую в хранилище по presigned URL, поэтому лимит 1 МБ на API разумен (5.1).
    Путь, где допустима форма (например, отписка по RFC 8058), перечисляется в `non_json_paths`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        non_json_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._non_json_paths = frozenset(non_json_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        length_header = headers.get("content-length")
        has_body = (length_header not in (None, "0")) or "chunked" in headers.get(
            "transfer-encoding", ""
        ).lower()

        if length_header is not None:
            try:
                declared = int(length_header)
            except ValueError:
                await self._reject(ErrorCode.INVALID_REQUEST, scope, receive, send)
                return
            if declared > self._max_body_bytes:
                await self._reject(ErrorCode.PAYLOAD_TOO_LARGE, scope, receive, send)
                return

        if has_body and scope["path"] not in self._non_json_paths:
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                await self._reject(ErrorCode.UNSUPPORTED_MEDIA_TYPE, scope, receive, send)
                return

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise _PayloadTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _PayloadTooLargeError:
            if response_started:
                raise
            await self._reject(ErrorCode.PAYLOAD_TOO_LARGE, scope, receive, send)

    @staticmethod
    async def _reject(code: ErrorCode, scope: Scope, receive: Receive, send: Send) -> None:
        await problem_response(code, scope)(scope, receive, send)

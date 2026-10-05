"""Ответы problem+json, request_id и защита входа: на мини-приложении с настоящими обработчиками."""

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request

from messunjerr.core.codes import PROBLEM_SPECS, ErrorCode
from messunjerr.core.errors import DomainError, NotFoundError, RateLimitedError
from messunjerr.core.logs import configure_logging
from messunjerr.core.middleware import RequestContextMiddleware, RequestGuardMiddleware
from messunjerr.core.pagination import Limit
from messunjerr.core.problems import PROBLEM_MEDIA_TYPE, install_problem_handlers
from messunjerr.core.schemas import ApiModel

SECRET_INPUT = "super-secret-value-that-must-never-be-echoed"
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class Item(ApiModel):
    name: str
    qty: int


def build_app() -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.add_middleware(RequestGuardMiddleware, max_body_bytes=1024, non_json_paths={"/form"})
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/domain-missing")
    async def domain_missing() -> None:
        raise NotFoundError

    @app.get("/domain-limited")
    async def domain_limited() -> None:
        raise RateLimitedError(retry_after=30)

    @app.get("/domain-custom")
    async def domain_custom() -> None:
        raise DomainError(ErrorCode.COMMENTS_FORBIDDEN, "Author disabled comments", reason="policy")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_INPUT)

    @app.post("/items")
    async def create(item: Item) -> Item:
        return item

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/form")
    async def form(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.get("/page")
    async def page(limit: Limit = 20) -> dict[str, int]:
        return {"limit": limit}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def assert_problem(response: httpx.Response, code: ErrorCode, *, path: str) -> dict[str, Any]:
    status, title = PROBLEM_SPECS[code]
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["type"] == f"/problems/{code.value}"
    assert body["title"] == title
    assert body["status"] == status
    assert body["code"] == code.value
    assert body["instance"] == path
    assert body["request_id"] == response.headers["x-request-id"]
    assert isinstance(body["detail"], str)
    assert body["detail"]
    return body


# ----------------------------------------------------------------------------- ошибки
async def test_unknown_path_is_problem_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert_problem(response, ErrorCode.NOT_FOUND, path="/nope")


async def test_wrong_method_is_405_with_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/ok")
    assert_problem(response, ErrorCode.METHOD_NOT_ALLOWED, path="/ok")
    assert "GET" in response.headers["allow"]


async def test_domain_error_uses_catalog_status(client: httpx.AsyncClient) -> None:
    response = await client.get("/domain-missing")
    assert_problem(response, ErrorCode.NOT_FOUND, path="/domain-missing")


async def test_domain_error_carries_headers_and_extensions(client: httpx.AsyncClient) -> None:
    limited = await client.get("/domain-limited")
    body = assert_problem(limited, ErrorCode.RATE_LIMITED, path="/domain-limited")
    assert limited.headers["retry-after"] == "30"
    assert body["retry_after"] == 30

    custom = await client.get("/domain-custom")
    body = assert_problem(custom, ErrorCode.COMMENTS_FORBIDDEN, path="/domain-custom")
    assert body["detail"] == "Author disabled comments"
    assert body["reason"] == "policy"


async def test_unhandled_exception_hides_details(client: httpx.AsyncClient) -> None:
    response = await client.get("/boom")
    assert_problem(response, ErrorCode.INTERNAL_ERROR, path="/boom")
    assert SECRET_INPUT not in response.text


# ----------------------------------------------------------------------------- валидация
async def test_validation_error_lists_items_without_input_echo(client: httpx.AsyncClient) -> None:
    response = await client.post("/items", json={"name": SECRET_INPUT, "qty": "many", "x": 1})
    body = assert_problem(response, ErrorCode.VALIDATION_ERROR, path="/items")
    items = {(item["pointer"], item["code"]) for item in body["errors"]}
    assert ("/body/qty", "invalid_format") in items
    assert ("/body/x", "unknown_field") in items
    assert SECRET_INPUT not in response.text


async def test_validation_error_for_missing_field(client: httpx.AsyncClient) -> None:
    response = await client.post("/items", json={"qty": 1})
    body = assert_problem(response, ErrorCode.VALIDATION_ERROR, path="/items")
    assert body["errors"][0]["pointer"] == "/body/name"
    assert body["errors"][0]["code"] == "required"


@pytest.mark.parametrize("limit", ["0", "101", "-5"])
async def test_limit_out_of_range(client: httpx.AsyncClient, limit: str) -> None:
    response = await client.get("/page", params={"limit": limit})
    body = assert_problem(response, ErrorCode.VALIDATION_ERROR, path="/page")
    assert body["errors"][0]["pointer"] == "/query/limit"
    assert body["errors"][0]["code"] == "out_of_range"


async def test_limit_not_a_number(client: httpx.AsyncClient) -> None:
    response = await client.get("/page", params={"limit": "abc"})
    body = assert_problem(response, ErrorCode.VALIDATION_ERROR, path="/page")
    assert body["errors"][0]["code"] == "invalid_format"


async def test_broken_json_is_400_invalid_request(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/items", content=b'{"name": ', headers={"content-type": "application/json"}
    )
    assert_problem(response, ErrorCode.INVALID_REQUEST, path="/items")


# ----------------------------------------------------------------------------- request_id
async def test_request_id_is_generated_and_returned(client: httpx.AsyncClient) -> None:
    response = await client.get("/ok")
    assert HEX32.match(response.headers["x-request-id"])


async def test_valid_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/ok", headers={"X-Request-ID": "trace-123.abc_X"})
    assert response.headers["x-request-id"] == "trace-123.abc_X"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "кириллица", "a/b", ""])
async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    # Значение передаём байтами: httpx не умеет кодировать не-ASCII строки в заголовках.
    response = await client.get("/ok", headers={b"X-Request-ID": bad.encode("utf-8")})
    assert HEX32.match(response.headers["x-request-id"])


async def test_error_body_carries_the_incoming_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope", headers={"X-Request-ID": "from-client"})
    body = assert_problem(response, ErrorCode.NOT_FOUND, path="/nope")
    assert body["request_id"] == "from-client"


# ----------------------------------------------------------------------------- защита входа
async def test_declared_body_over_limit_is_413(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/echo", content=b"x" * 2048, headers={"content-type": "application/json"}
    )
    assert_problem(response, ErrorCode.PAYLOAD_TOO_LARGE, path="/echo")


async def test_streamed_body_over_limit_is_413(client: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b"y" * 512

    response = await client.post(
        "/echo", content=chunks(), headers={"content-type": "application/json"}
    )
    assert_problem(response, ErrorCode.PAYLOAD_TOO_LARGE, path="/echo")


async def test_body_within_limit_passes(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/echo", content=b"z" * 1024, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"bytes": 1024}


async def test_non_json_body_is_415(client: httpx.AsyncClient) -> None:
    response = await client.post("/echo", content=b"hello", headers={"content-type": "text/plain"})
    assert_problem(response, ErrorCode.UNSUPPORTED_MEDIA_TYPE, path="/echo")


async def test_json_with_charset_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/echo", content=b"{}", headers={"content-type": "application/json; charset=utf-8"}
    )
    assert response.status_code == 200


async def test_empty_post_without_content_type_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post("/echo")
    assert response.status_code == 200
    assert response.json() == {"bytes": 0}


async def test_form_is_allowed_only_on_listed_paths(client: httpx.AsyncClient) -> None:
    body = b"List-Unsubscribe=One-Click"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    allowed = await client.post("/form", headers=headers, content=body)
    rejected = await client.post("/echo", headers=headers, content=body)
    assert allowed.status_code == 200
    assert_problem(rejected, ErrorCode.UNSUPPORTED_MEDIA_TYPE, path="/echo")


# ----------------------------------------------------------------------------- access-лог
@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[Any]:
    configure_logging("DEBUG", "json", cache_loggers=False)

    def read() -> list[dict[str, Any]]:
        lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        return [json.loads(line) for line in lines]

    yield read
    structlog.reset_defaults()


async def test_access_log_has_fields_and_no_query_string(
    client: httpx.AsyncClient, json_logs: Any
) -> None:
    response = await client.get("/page", params={"limit": 5, "ticket": "must-not-leak"})
    record = next(r for r in json_logs() if r["event"] == "http_request")
    assert record["method"] == "GET"
    assert record["path"] == "/page"
    assert record["status"] == 200
    assert record["request_id"] == response.headers["x-request-id"]
    assert isinstance(record["duration_ms"], float)
    assert record["level"] == "info"
    assert "ts" in record
    assert "must-not-leak" not in json.dumps(record)


async def test_health_requests_are_logged_at_debug(
    client: httpx.AsyncClient, json_logs: Any
) -> None:
    await client.get("/health/live")
    record = next(r for r in json_logs() if r["event"] == "http_request")
    assert record["level"] == "debug"


async def test_failed_request_is_logged_as_error(client: httpx.AsyncClient, json_logs: Any) -> None:
    await client.get("/boom")
    records = json_logs()
    access = next(r for r in records if r["event"] == "http_request")
    assert access["status"] == 500
    assert access["level"] == "error"
    assert any(r["event"] == "unhandled_exception" for r in records)

"""Лимиты запросов (конфиг, результат, ошибка 429), защита от CSRF, отпечаток идемпотентного запроса,
подписи устройств и маски адресов: всё, что проверяется без Redis и БД."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import SecretStr

from messunjerr.core.codes import ErrorCode
from messunjerr.core.csrf import CSRF_HEADER, CSRF_VALUE, require_csrf_guard
from messunjerr.core.errors import DomainError
from messunjerr.core.idempotency import parse_key, request_fingerprint
from messunjerr.core.problems import install_problem_handlers
from messunjerr.core.ratelimit import (
    BucketConfig,
    RateLimitResult,
    load_buckets,
    most_restrictive,
    parse_buckets,
    parse_window,
    rate_limited,
)
from messunjerr.core.ratelimit_deps import client_ip, subject_digest
from messunjerr.identity.domain.devices import describe_user_agent, mask_ip
from messunjerr.identity.domain.emails import mask_email
from messunjerr.settings import AppEnv, Settings


# ----------------------------------------------------------------------------- окна и конфиг
@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30s", 30), ("10m", 600), ("1h", 3600), ("2d", 172_800), (" 15m ", 900)],
)
def test_window_parsing(value: str, seconds: int) -> None:
    assert parse_window(value) == seconds


@pytest.mark.parametrize("value", ["", "10", "m", "0m", "1w", "-5m", "1.5h", "10 m"])
def test_invalid_windows_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="окно лимита"):
        parse_window(value)


def test_default_buckets_match_the_specification_table() -> None:
    """Значения из таблицы 4.14 спецификации."""
    buckets = load_buckets()
    expected = {
        "auth_login_ip": (20, 600),
        "auth_login_account": (5, 900),
        "auth_register_ip": (5, 3600),
        "auth_email_ip": (10, 3600),
        "auth_email_addr": (3, 3600),
        "auth_refresh_session": (60, 60),
        "username_check_ip": (30, 60),
        "api_read": (600, 60),
        "api_write": (120, 60),
        "upload_init": (60, 3600),
    }
    assert {name: (b.limit, b.window_seconds) for name, b in buckets.items()} == expected


def test_buckets_that_guard_guessing_and_mail_fail_closed() -> None:
    buckets = load_buckets()
    closed = {name for name, bucket in buckets.items() if bucket.on_unavailable == "deny"}
    assert closed == {
        "auth_login_ip",
        "auth_login_account",
        "auth_register_ip",
        "auth_email_ip",
        "auth_email_addr",
    }


def test_refill_rate_is_limit_per_window() -> None:
    bucket = BucketConfig("x", limit=5, window_seconds=900)
    assert bucket.refill_per_second == pytest.approx(5 / 900)


def test_operator_file_overrides_defaults_field_by_field(tmp_path: Path) -> None:
    override = tmp_path / "ratelimits.toml"
    override.write_text(
        '[auth_login_ip]\nlimit = 50\n\n[brand_new]\nlimit = 7\nwindow = "30s"\n', encoding="utf-8"
    )

    buckets = load_buckets(override)

    assert buckets["auth_login_ip"].limit == 50
    assert buckets["auth_login_ip"].window_seconds == 600  # окно осталось от значения по умолчанию
    assert buckets["auth_login_ip"].on_unavailable == "deny"
    assert buckets["brand_new"] == BucketConfig("brand_new", 7, 30, "allow")
    assert buckets["api_read"].limit == 600  # остальное не тронуто


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"x": {"limit": 0, "window": "1m"}}, "limit"),
        ({"x": {"limit": "5", "window": "1m"}}, "limit"),
        ({"x": {"limit": True, "window": "1m"}}, "limit"),
        ({"x": {"limit": 5}}, "window"),
        ({"x": {"limit": 5, "window": "soon"}}, "окно"),
        ({"x": {"limit": 5, "window": "1m", "on_unavailable": "maybe"}}, "allow или deny"),
        ({"x": {"limit": 5, "window": "1m", "burst": 9}}, "неизвестные поля"),
        ({"x": 5}, "таблицей"),
    ],
)
def test_broken_configuration_is_rejected(table: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_buckets(table)


# ----------------------------------------------------------------------------- результат и ошибка
def result(
    *, remaining: int, limit: int = 10, allowed: bool = True, retry_after: int = 0
) -> RateLimitResult:
    return RateLimitResult("b", allowed, limit, remaining, retry_after, reset=30)


def test_headers_follow_the_specification() -> None:
    assert result(remaining=7).headers() == {
        "RateLimit-Limit": "10",
        "RateLimit-Remaining": "7",
        "RateLimit-Reset": "30",
    }


def test_429_carries_retry_after_everywhere() -> None:
    error = rate_limited(result(remaining=0, allowed=False, retry_after=42))

    assert isinstance(error, DomainError)
    assert error.code is ErrorCode.RATE_LIMITED
    assert error.status == 429
    assert error.headers["Retry-After"] == "42"
    assert error.headers["RateLimit-Remaining"] == "0"
    assert error.extensions == {"retry_after": 42}


def test_retry_after_is_never_zero() -> None:
    assert (
        rate_limited(result(remaining=0, allowed=False, retry_after=0)).headers["Retry-After"]
        == "1"
    )


def test_the_tightest_bucket_is_reported() -> None:
    roomy = result(remaining=900, limit=1000)
    tight = result(remaining=1, limit=20)
    assert most_restrictive([roomy, tight]) is tight


def test_subject_digest_hides_the_value_and_ignores_case() -> None:
    first = subject_digest("Person@Example.com")
    assert first == subject_digest("person@example.COM")
    assert first != subject_digest("other@example.com")
    assert "person" not in first
    assert len(first) == 24


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8::1", "2001:db8::1"),
        ("testclient", "unknown"),
        (None, "unknown"),
    ],
)
def test_client_ip_for_limits(host: str | None, expected: str) -> None:
    fake = SimpleNamespace(client=SimpleNamespace(host=host) if host is not None else None)
    assert client_ip(cast("Request", fake)) == expected


# ----------------------------------------------------------------------------- CSRF
def csrf_app(*, app_env: AppEnv = "dev") -> FastAPI:
    app = FastAPI()
    settings = Settings(  # pyright: ignore[reportCallIssue]
        database_url=SecretStr("postgresql+asyncpg://app:p@h/db"),
        redis_url=SecretStr("redis://h/0"),
        app_env=app_env,
        public_base_url="https://chat.example.ru",
        allowed_origins=["http://localhost:3000/"],
    )
    app.state.resources = SimpleNamespace(settings=settings)
    install_problem_handlers(app)

    @app.post("/guarded", dependencies=[Depends(require_csrf_guard)])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def post(app: FastAPI, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/guarded", headers=headers)


GOOD = {CSRF_HEADER: CSRF_VALUE}


async def test_csrf_header_is_mandatory() -> None:
    assert (await post(csrf_app(), GOOD)).status_code == 200
    for headers in ({}, {CSRF_HEADER: "other"}, {CSRF_HEADER: ""}):
        response = await post(csrf_app(), headers)
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_failed"


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://chat.example.ru", True),
        ("https://chat.example.ru/", True),
        ("http://localhost:3000", True),  # из ALLOWED_ORIGINS (слэш на конце снимается)
        ("https://evil.example", False),
        ("http://chat.example.ru", False),  # другая схема: другой источник
        ("https://chat.example.ru.evil.example", False),
        ("null", False),
    ],
)
async def test_origin_must_be_the_client_or_an_allowed_one(origin: str, allowed: bool) -> None:
    response = await post(csrf_app(), {**GOOD, "Origin": origin})
    assert (response.status_code == 200) is allowed
    if not allowed:
        assert response.json()["code"] == "csrf_failed"


async def test_a_request_without_origin_passes_when_the_header_is_there() -> None:
    """Не браузер (curl, мобильный клиент): `Origin` не шлёт, но и чужим сайтом быть не может."""
    assert (await post(csrf_app(), GOOD)).status_code == 200


@pytest.mark.parametrize(
    ("site", "dev_ok", "prod_ok"),
    [
        ("same-origin", True, True),
        ("none", True, True),
        # Клиент :3000 и API :8000 в разработке: один сайт, но разные источники.
        ("same-site", True, False),
        ("cross-site", False, False),
        ("SAME-ORIGIN", True, True),
    ],
)
async def test_fetch_site_metadata(site: str, dev_ok: bool, prod_ok: bool) -> None:
    headers = {**GOOD, "Sec-Fetch-Site": site}
    assert (await post(csrf_app(app_env="dev"), headers)).status_code == (200 if dev_ok else 403)
    assert (await post(csrf_app(app_env="prod"), headers)).status_code == (200 if prod_ok else 403)


# ----------------------------------------------------------------------------- идемпотентность
def fake_request(method: str = "POST", path: str = "/api/v1/things", query: str = "") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
    }
    return Request(scope)


def test_fingerprint_ignores_json_formatting_and_key_order() -> None:
    first = request_fingerprint(fake_request(), b'{"a": 1, "b": [1, 2], "c": "\xd0\xb4"}')
    second = request_fingerprint(fake_request(), b'{"c":"\xd0\xb4","b":[1,2],"a":1}')
    assert first == second


@pytest.mark.parametrize(
    "other",
    [
        (fake_request(), b'{"a": 2}'),
        (fake_request(path="/api/v1/other"), b'{"a": 1}'),
        (fake_request(method="PUT"), b'{"a": 1}'),
        (fake_request(query="x=1"), b'{"a": 1}'),
    ],
    ids=["body", "path", "method", "query"],
)
def test_fingerprint_changes_with_anything_that_changes_the_request(
    other: tuple[Request, bytes],
) -> None:
    base = request_fingerprint(fake_request(), b'{"a": 1}')
    assert request_fingerprint(*other) != base


def test_fingerprint_of_a_non_json_body_is_still_stable() -> None:
    assert request_fingerprint(fake_request(), b"plain text") == request_fingerprint(
        fake_request(), b"plain text"
    )
    assert len(request_fingerprint(fake_request(), b"")) == 32


def test_query_order_does_not_matter() -> None:
    assert request_fingerprint(fake_request(query="a=1&b=2"), b"") == request_fingerprint(
        fake_request(query="b=2&a=1"), b""
    )


def test_idempotency_key_must_be_a_uuid() -> None:
    assert (
        parse_key(" 0192B7A0-5C1E-7C3A-9D54-3F1A2B6C7D80 ")
        == "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80"
    )
    with pytest.raises(DomainError) as caught:
        parse_key("not-a-uuid")
    (item,) = caught.value.errors
    assert (item.pointer, item.code) == ("/header/Idempotency-Key", "invalid_format")


# ----------------------------------------------------------------------------- устройства и маски
@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
            "Firefox на Windows",
        ),
        (
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
            ),
            "Edge на Windows",
        ),
        (
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
            ),
            "Safari на macOS",
        ),
        (
            (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Mobile Safari/537.36"
            ),
            "Chrome на Android",
        ),
        (
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "Safari на iOS",
        ),
        ("curl/8.5.0", "curl"),
        ("python-httpx/0.28.1", "Python"),
        ("SomethingUnknown/1.0", None),
        ("", None),
        (None, None),
    ],
)
def test_user_agent_description(user_agent: str | None, expected: str | None) -> None:
    assert describe_user_agent(user_agent) == expected


@pytest.mark.parametrize(
    ("ip", "masked"),
    [
        ("203.0.113.45", "203.0.113.x"),
        ("2001:db8:85a3:8d3:1319:8a2e:370:7344", "2001:db8:85a3::x"),
        ("::1", "::x"),
        ("not-an-ip", None),
        ("", None),
        (None, None),
    ],
)
def test_ip_masking(ip: str | None, masked: str | None) -> None:
    assert mask_ip(ip) == masked


@pytest.mark.parametrize(
    ("address", "masked"),
    [("ivan.petrov@example.com", "i***@example.com"), ("a@b.ru", "a***@b.ru"), ("broken", "***")],
)
def test_email_masking(address: str, masked: str) -> None:
    assert mask_email(address) == masked


async def test_a_problem_response_is_never_cacheable() -> None:
    """Ответ об ошибке содержит `request_id` и состояние аккаунта: кэшировать его нельзя."""
    app = FastAPI()
    install_problem_handlers(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/nothing-here")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.text)["code"] == "not_found"

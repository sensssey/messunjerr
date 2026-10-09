"""Тесты prod-подобного стенда (S4): ходят на https://messunjerr.localhost через Caddy.

Запуск: `./dev.ps1 stand-test` (контейнер stand-tools подключён к сетям стенда, доверяет корневому
сертификату Caddy и видит внутренний адрес SeaweedFS). Без STAND_BASE_URL тесты пропускаются, как
интеграционные без базы; тесты с меткой `offline` стенду не нужны.
"""

import asyncio
import os
import re
import ssl
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio

STAND_BASE_URL = os.environ.get("STAND_BASE_URL")


@dataclass(frozen=True, slots=True)
class Stand:
    base_url: str
    host: str
    ca_file: str
    ssl_context: ssl.SSLContext
    access_log: Path
    s3_access_key: str
    s3_secret_key: str
    internal_s3_url: str
    idle_seconds: float

    @property
    def ws_url(self) -> str:
        return self.base_url.replace("https://", "wss://", 1)


@pytest.fixture(autouse=True)
def _require_stand(request: pytest.FixtureRequest) -> None:
    if "offline" in request.keywords:  # имена меток входят в keywords
        return
    if not STAND_BASE_URL:
        pytest.skip("нужен запущенный стенд: ./dev.ps1 up-prod-like, затем ./dev.ps1 stand-test")


def _read(name: str) -> str:
    return Path(os.environ[name]).read_text(encoding="utf-8").strip()


@pytest.fixture(scope="session")
def stand() -> Stand:
    # Фикстура уровня сессии создаётся раньше `_require_stand`, поэтому пропуск нужен и здесь.
    if not STAND_BASE_URL:
        pytest.skip("нужен запущенный стенд: ./dev.ps1 up-prod-like, затем ./dev.ps1 stand-test")
    ca_file = os.environ["STAND_CA_FILE"]
    return Stand(
        base_url=STAND_BASE_URL.rstrip("/"),
        host=urlsplit(STAND_BASE_URL).hostname or "",
        ca_file=ca_file,
        ssl_context=ssl.create_default_context(cafile=ca_file),
        access_log=Path(os.environ.get("STAND_ACCESS_LOG", "/var/log/caddy/access.log")),
        s3_access_key=_read("STAND_S3_ACCESS_KEY_FILE"),
        s3_secret_key=_read("STAND_S3_SECRET_KEY_FILE"),
        internal_s3_url=os.environ.get("STAND_INTERNAL_S3_URL", "http://seaweedfs:8333"),
        idle_seconds=float(os.environ.get("STAND_IDLE_SECONDS", "8")),
    )


def raw_request(client: httpx.AsyncClient, target: str) -> httpx.Request:
    """GET с адресом «как есть»: httpx сам схлопывает `..` и `//`, а расширение `target` нет."""
    return client.build_request("GET", "/placeholder", extensions={"target": target.encode()})


@pytest_asyncio.fixture
async def client(stand: Stand) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=stand.base_url, verify=stand.ssl_context, timeout=60
    ) as http:
        yield http


# ----------------------------------------------------------------------------- учётные записи (S5)
PASSWORD = "correct horse battery staple"
RESET_HINT = (
    "лимит запросов стенда исчерпан (регистраций с одного адреса 5 в час, заявок на загрузку 60 в час "
    "на человека, заявок в друзья 30 в сутки, подписок 100 в час, поисков людей 30 в минуту): "
    "подождите или сбросьте счётчики командой `make stand-reset-limits`"
)


@dataclass(frozen=True, slots=True)
class Account:
    """Подтверждённый аккаунт стенда (создан настоящим API: регистрация и письмо из Mailpit)."""

    user_id: str
    email: str
    headers: dict[str, str]


async def _verification_token(mailpit: httpx.AsyncClient, email: str, since: datetime) -> str:
    """Токен из письма подтверждения, пришедшего после `since`: письма прошлых прогонов не годятся."""
    for _ in range(60):
        found = await mailpit.get("/api/v1/search", params={"query": f"to:{email}"})
        found.raise_for_status()
        messages: list[dict[str, Any]] = found.json().get("messages") or []
        fresh = [
            message
            for message in messages
            if datetime.fromisoformat(str(message["Created"]).replace("Z", "+00:00")) >= since
        ]
        if fresh:
            message = await mailpit.get(f"/api/v1/message/{fresh[0]['ID']}")
            message.raise_for_status()
            token = re.search(r"#token=(\S+)", message.json()["Text"])
            assert token, "в письме нет ссылки с токеном"
            return token.group(1)
        await asyncio.sleep(0.5)
    raise AssertionError(f"письмо для {email} не пришло в Mailpit за 30 секунд")


async def _sign_in(stand: Stand, name: str) -> Account:
    """Постоянный аккаунт стенда: первый прогон регистрирует его, следующие только входят.

    Так прогоны тестов не упираются в лимит регистраций (5 в час с одного адреса)."""
    mailpit_url = os.environ.get("STAND_MAILPIT_URL")
    if not mailpit_url:
        pytest.skip(
            "нужен STAND_MAILPIT_URL (Mailpit стенда): запускайте через ./dev.ps1 stand-test"
        )
    email = f"stand-{name}@example.com"
    credentials: dict[str, Any] = {"login": email, "password": PASSWORD}
    async with (
        httpx.AsyncClient(base_url=stand.base_url, verify=stand.ssl_context, timeout=60) as api,
        httpx.AsyncClient(base_url=mailpit_url, timeout=15) as mailpit,
    ):
        auth = await api.post("/api/v1/auth/login", json=credentials)
        if auth.status_code != 200:
            started = datetime.now(UTC) - timedelta(seconds=5)
            registered = await api.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "username": f"stand_{name}",
                    "password": PASSWORD,
                    "accept_terms": True,
                },
            )
            if registered.status_code == 429:
                pytest.fail(RESET_HINT)
            assert registered.status_code == 201, registered.text
            token = await _verification_token(mailpit, email, started)
            verified = await api.post("/api/v1/auth/verify-email", json={"token": token})
            assert verified.status_code == 200, verified.text
            auth = verified
        body = auth.json()
    return Account(
        user_id=str(body["user"]["id"]),
        email=email,
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )


@pytest_asyncio.fixture(scope="session")
async def account(stand: Stand) -> Account:
    return await _sign_in(stand, "owner")


@pytest_asyncio.fixture(scope="session")
async def other_account(stand: Stand) -> Account:
    """Второй человек: чужие ресурсы для него не существуют."""
    return await _sign_in(stand, "other")

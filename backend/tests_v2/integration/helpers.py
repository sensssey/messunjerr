"""Помощники интеграционных тестов identity: регистрация, токены из писем, вход, refresh, лимиты."""

import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.mail import render_email
from messunjerr.core.ratelimit import load_buckets
from messunjerr.main import create_app
from messunjerr.settings import Settings

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "another sturdy passphrase 42"
SENDER = "messunjerr <no-reply@messunjerr.local>"
REFRESH_COOKIE = "__Secure-mj_refresh"
CSRF = {"X-Requested-With": "messunjerr", "Origin": "http://localhost:3000"}
REFRESH = "/api/v1/auth/refresh"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/me"


async def fetch_all(engine: AsyncEngine, sql: str, **params: Any) -> list[dict[str, Any]]:
    """Строки как словари; запросы идут от суперпользователя, в обход прав приложения."""
    async with engine.connect() as connection:
        result = await connection.execute(text(sql), params)
        return [dict(row) for row in result.mappings()]


async def fetch_one(engine: AsyncEngine, sql: str, **params: Any) -> dict[str, Any]:
    rows = await fetch_all(engine, sql, **params)
    assert len(rows) == 1, f"ожидалась одна строка, получено {len(rows)}: {sql}"
    return rows[0]


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


def new_credentials(**overrides: Any) -> dict[str, Any]:
    """Тело регистрации с уникальными почтой и ником; поля можно переопределить."""
    unique = uuid.uuid4().hex[:12]
    body: dict[str, Any] = {
        "email": f"user-{unique}@example.com",
        "username": f"user_{unique}",
        "password": PASSWORD,
        "accept_terms": True,
    }
    body.update(overrides)
    return body


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def verification_token(jobs: InMemoryJobQueue, email: str) -> str:
    """Токен подтверждения из последнего письма `verify_email`, поставленного для `email`."""
    matching = [
        job
        for job in jobs.named("send_email")
        if job.kwargs["to"] == email and job.kwargs["template"] == "verify_email"
    ]
    assert matching, f"письмо подтверждения для {email} не поставлено в очередь"
    return str(matching[-1].kwargs["context"]["token"])


def token_from_rendered_email(jobs: InMemoryJobQueue, email: str) -> str:
    """Достаёт токен так, как это сделал бы человек: из текста письма, собранного по шаблону."""
    job = next(
        job
        for job in reversed(jobs.named("send_email"))
        if job.kwargs["to"] == email and job.kwargs["template"] == "verify_email"
    )
    message = render_email(
        job.kwargs["template"], job.kwargs["context"], sender=SENDER, to=job.kwargs["to"]
    )
    body = message.get_body(("plain",))
    assert body is not None
    found = re.search(r"#token=(\S+)", body.get_content())
    assert found, "в тексте письма нет ссылки с токеном"
    return found.group(1)


@dataclass(frozen=True, slots=True)
class SignedInUser:
    credentials: dict[str, Any]
    auth: dict[str, Any]
    response: httpx.Response

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.auth["access_token"])

    @property
    def user_id(self) -> str:
        return str(self.auth["user"]["id"])

    @property
    def refresh_token(self) -> str:
        return cookie_token(self.response)

    @property
    def session_id(self) -> str:
        return str(self.auth["session_id"])


def cookie_token(response: httpx.Response) -> str:
    """Refresh-токен из заголовка `Set-Cookie` ответа."""
    found = re.match(rf"{REFRESH_COOKIE}=([^;]+);", response.headers["set-cookie"])
    assert found, f"в ответе нет cookie с refresh-токеном: {response.headers.get('set-cookie')}"
    return found.group(1)


def cookie_header(token: str) -> dict[str, str]:
    """Cookie запроса. Передаём заголовком: `Secure`-cookie клиент по http сам бы не отправил."""
    return {"Cookie": f"{REFRESH_COOKIE}={token}"}


def refresh_headers(token: str, **extra: str) -> dict[str, str]:
    return {**CSRF, **cookie_header(token), **extra}


async def do_refresh(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(REFRESH, headers=refresh_headers(token))


async def login_again(client: httpx.AsyncClient, user: SignedInUser, **extra: Any) -> SignedInUser:
    """Ещё один вход тем же пользователем: вторая сессия (второе устройство)."""
    response = await client.post(
        LOGIN,
        json={"login": user.credentials["email"], "password": PASSWORD},
        headers=extra.pop("headers", None),
    )
    assert response.status_code == 200, response.text
    return SignedInUser(credentials=user.credentials, auth=response.json(), response=response)


def email_jobs(jobs: InMemoryJobQueue, template: str, to: str | None = None) -> list[Any]:
    """Письма `template`, поставленные в очередь (для `to`, если задан)."""
    return [
        job
        for job in jobs.named("send_email")
        if job.kwargs["template"] == template and (to is None or job.kwargs["to"] == to)
    ]


def token_in_email(jobs: InMemoryJobQueue, template: str, to: str) -> str:
    """Токен из последнего письма `template` для `to`."""
    matching = email_jobs(jobs, template, to)
    assert matching, f"письмо {template} для {to} не поставлено в очередь"
    return str(matching[-1].kwargs["context"]["token"])


@asynccontextmanager
async def limited_client(
    test_settings: Settings,
    jobs: InMemoryJobQueue,
    *,
    windows: dict[str, int] | None = None,
    **limits: int,
) -> AsyncGenerator[tuple[FastAPI, httpx.AsyncClient]]:
    """Приложение с включёнными лимитами; `limits` меняют ёмкость бакетов, `windows` их окна (секунды)."""
    buckets = {
        name: replace(
            config,
            limit=limits.get(name, config.limit),
            window_seconds=(windows or {}).get(name, config.window_seconds),
        )
        for name, config in load_buckets().items()
    }
    settings = test_settings.model_copy(update={"rate_limits_enabled": True})
    application = create_app(settings, job_queue=jobs, rate_limits=buckets)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield application, http


@asynccontextmanager
async def client_with(
    test_settings: Settings, jobs: InMemoryJobQueue, **overrides: Any
) -> AsyncGenerator[httpx.AsyncClient]:
    """Приложение с другими настройками (`min_age`, паузой смены ника и т.п.); лимиты остаются выключены."""
    application = create_app(test_settings.model_copy(update=overrides), job_queue=jobs)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def register(
    client: httpx.AsyncClient, **overrides: Any
) -> tuple[httpx.Response, dict[str, Any]]:
    body = new_credentials(**overrides)
    return await client.post("/api/v1/auth/register", json=body), body


async def verified_user(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, **overrides: Any
) -> SignedInUser:
    """Регистрирует аккаунт и подтверждает почту: пользователь `active` и уже вошёл."""
    response, body = await register(client, **overrides)
    assert response.status_code == 201, response.text
    token = verification_token(jobs, body["email"])
    verify = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert verify.status_code == 200, verify.text
    return SignedInUser(credentials=body, auth=verify.json(), response=verify)


# ----------------------------------------------------------------------------- профили (S3)
def url(ref: str) -> str:
    return f"/api/v1/users/{ref}"


async def fill_profile(
    client: httpx.AsyncClient, user: SignedInUser, **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "display_name": "Анна",
        "bio": "Люблю горы",
        "links": [{"title": "Блог", "url": "https://example.com"}],
        "birth_date": "1990-05-12",
        "birth_date_visibility": "full",
        "city": "Казань",
        "language": "ru",
        "timezone": "Europe/Moscow",
    }
    body.update(overrides)
    response = await client.patch("/api/v1/me/profile", json=body, headers=user.headers)
    assert response.status_code == 200, response.text
    profile: dict[str, Any] = response.json()
    return profile


async def set_privacy(client: httpx.AsyncClient, user: SignedInUser, **body: Any) -> None:
    response = await client.patch("/api/v1/me/privacy", json=body, headers=user.headers)
    assert response.status_code == 200, response.text


async def view(client: httpx.AsyncClient, viewer: SignedInUser, ref: str) -> httpx.Response:
    return await client.get(url(ref), headers=viewer.headers)


async def two_users(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, **owner_profile: Any
) -> tuple[SignedInUser, SignedInUser]:
    owner = await verified_user(client, jobs)
    await fill_profile(client, owner, **owner_profile)
    viewer = await verified_user(client, jobs)
    return owner, viewer

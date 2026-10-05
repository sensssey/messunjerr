"""Помощники интеграционных тестов identity: регистрация, токен из письма, вход."""

import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.mail import render_email

PASSWORD = "correct horse battery staple"
SENDER = "messunjerr <no-reply@messunjerr.local>"
REFRESH_COOKIE = "__Secure-mj_refresh"


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
        cookie = self.response.headers["set-cookie"]
        found = re.match(rf"{REFRESH_COOKIE}=([^;]+);", cookie)
        assert found
        return found.group(1)


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

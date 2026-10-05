"""Сквозной путь письма: API → arq (Redis) → воркер → SMTP → Mailpit → человек подтверждает почту.

Нужны Mailpit и его HTTP API: переменные `SMTP_URL` и `MAILPIT_API_URL` задаёт Compose (`tools`)
и CI. Без них тест пропускается.
"""

import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

from messunjerr.core.jobs import QUEUE_EMAIL
from messunjerr.jobs.worker import build_worker
from messunjerr.main import create_app
from messunjerr.settings import Settings

from .helpers import PASSWORD, bearer, new_credentials


@pytest_asyncio.fixture
async def mailbox() -> AsyncIterator[httpx.AsyncClient]:
    api_url = os.environ.get("MAILPIT_API_URL")
    if not api_url or not os.environ.get("SMTP_URL"):
        pytest.skip("нужны SMTP_URL и MAILPIT_API_URL (Mailpit): запускайте через Compose или CI")
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as http:
        yield http


async def messages_for(mailbox: httpx.AsyncClient, address: str) -> list[dict[str, Any]]:
    response = await mailbox.get("/api/v1/search", params={"query": f"to:{address}"})
    response.raise_for_status()
    found: list[dict[str, Any]] = response.json()["messages"] or []
    return found


async def test_registration_email_travels_through_arq_and_smtp(
    test_settings: Settings, mailbox: httpx.AsyncClient
) -> None:
    if test_settings.smtp_url is None:
        pytest.skip("SMTP_URL не задан")
    credentials = new_credentials()
    # Настоящая очередь: приложение без подставной `job_queue` ставит задачи в Redis через arq.
    application = create_app(test_settings)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201

            # Воркер разбирает очередь и выходит (burst): письмо уходит в Mailpit по SMTP.
            worker = build_worker(QUEUE_EMAIL, test_settings, burst=True, handle_signals=False)
            await worker.async_run()
            await worker.close()

            found = await messages_for(mailbox, credentials["email"])
            assert len(found) == 1
            try:
                message = (await mailbox.get(f"/api/v1/message/{found[0]['ID']}")).json()
                assert message["Subject"] == "Подтвердите адрес электронной почты в messunjerr"
                assert message["From"]["Address"] == "no-reply@messunjerr.local"
                assert [to["Address"] for to in message["To"]] == [credentials["email"]]
                assert "Здравствуйте" in message["Text"]
                assert "<a href=" in message["HTML"]
                token = re.search(r"#token=(\S+)", message["Text"])
                assert token

                verified = await client.post(
                    "/api/v1/auth/verify-email", json={"token": token.group(1)}
                )
                assert verified.status_code == 200
                me = await client.get("/api/v1/me", headers=bearer(verified.json()["access_token"]))
                assert me.json()["email"] == credentials["email"]
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"login": credentials["email"], "password": PASSWORD},
                )
                assert login.status_code == 200
            finally:
                await mailbox.request("DELETE", "/api/v1/messages", json={"IDs": [found[0]["ID"]]})

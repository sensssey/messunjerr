import os
import tempfile
from pathlib import Path

# Окружение должно быть задано до импорта приложения: src.config читает его при импорте
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db")

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from src.database import Base, engine  # noqa: E402
from src.main import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """Каждый тест работает с чистой БД."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest_asyncio.fixture
async def make_user(client):
    """Регистрирует пользователя и возвращает заголовки с его токеном."""
    async def _make_user(username="alice", password="password-1"):
        response = await client.post("/auth/register", json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    return _make_user

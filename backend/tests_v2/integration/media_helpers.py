"""Помощники интеграционных тестов медиа: заявка, «загрузка клиентом», завершение, чтение, удаление."""

import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.media.domain.ports import StoredObject
from messunjerr.media.infra.memory import InMemoryObjectStorage

from .helpers import SignedInUser

MEDIA = "/api/v1/media"
UPLOADS = f"{MEDIA}/uploads"
MIB = 1024 * 1024

# Первые байты настоящих файлов: хватает, чтобы заглушка обработки (сигнатуры) вынесла вердикт.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 100
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 100
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
PDF = b"%PDF-1.7\n" + b"x" * 100
EXE = (
    b"MZ"
    + b"\x90" * 58
    + (0x80).to_bytes(4, "little")
    + b"\x00" * 64
    + b"PE\x00\x00"
    + b"\x00" * 32
)


def body_of(**overrides: Any) -> dict[str, Any]:
    """Тело заявки: фото для поста; поля можно переопределить."""
    body: dict[str, Any] = {
        "purpose": "post",
        "filename": "photo.jpg",
        "content_type": "image/jpeg",
        "size_bytes": 1000,
    }
    body.update(overrides)
    return body


async def start(client: httpx.AsyncClient, user: SignedInUser, **overrides: Any) -> httpx.Response:
    return await client.post(UPLOADS, json=body_of(**overrides), headers=user.headers)


async def started(
    client: httpx.AsyncClient, user: SignedInUser, **overrides: Any
) -> dict[str, Any]:
    response = await start(client, user, **overrides)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def key_of(created: dict[str, Any]) -> str:
    return f"uploads/{created['asset']['id']}/original"


def put_object(
    storage: InMemoryObjectStorage,
    created: dict[str, Any],
    body: bytes,
    content_type: str | None = None,
) -> None:
    """Клиент загрузил файл по presigned-ссылке: объект появился в хранилище."""
    storage.put(key_of(created), body, content_type or created["upload"]["headers"]["Content-Type"])


async def complete(
    client: httpx.AsyncClient, user: SignedInUser, asset_id: str | uuid.UUID
) -> httpx.Response:
    return await client.post(f"{MEDIA}/uploads/{asset_id}/complete", headers=user.headers)


async def read_asset(
    client: httpx.AsyncClient, user: SignedInUser, asset_id: str | uuid.UUID
) -> httpx.Response:
    return await client.get(f"{MEDIA}/{asset_id}", headers=user.headers)


async def uploaded(
    client: httpx.AsyncClient,
    user: SignedInUser,
    storage: InMemoryObjectStorage,
    body: bytes = JPEG,
    **overrides: Any,
) -> dict[str, Any]:
    """Заявка, загрузка и завершение: ресурс в состоянии `uploaded`. Возвращает ответ заявки."""
    overrides.setdefault("size_bytes", len(body))
    created = await started(client, user, **overrides)
    put_object(storage, created, body)
    response = await complete(client, user, created["asset"]["id"])
    assert response.status_code == 202, response.text
    return created


class ProbingStorage(InMemoryObjectStorage):
    """Пока хранилище «отвечает», пул из одного соединения должен быть свободен для других запросов.

    Каждое обращение к хранилищу делает пробный запрос к БД через `sessions` (фикстура
    `single_connection_sessions`): если вызывающий код держит единственное соединение пула, проба
    упирается в тайм-аут пула и тест падает.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self.sessions = sessions
        self.probes = 0

    async def probe(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))
        self.probes += 1

    async def head(self, key: str) -> StoredObject | None:
        await self.probe()
        return await super().head(key)

    async def delete_many(self, keys: Sequence[str]) -> None:
        await self.probe()
        await super().delete_many(keys)

"""Помощники интеграционных тестов медиа: заявка, «загрузка клиентом», завершение, обработка, чтение.

Картинки настоящие (их строит Pillow): обработка S6 открывает файл целиком, а не только сигнатуру.
"""

import io
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from PIL import Image, ImageDraw
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.media.commands.process_media import Outcome, ProcessMedia, process_media
from messunjerr.media.domain.ports import StoredObject
from messunjerr.media.infra.images import DecodeBudget
from messunjerr.media.infra.memory import InMemoryObjectStorage

from .helpers import SignedInUser

MEDIA = "/api/v1/media"
UPLOADS = f"{MEDIA}/uploads"
MIB = 1024 * 1024
Sessions = async_sessionmaker[AsyncSession]

RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)


def quadrants(size: tuple[int, int] = (48, 32)) -> Image.Image:
    """Четыре цвета по четвертям: по ним видно поворот и обрезку."""
    width, height = size
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width // 2 - 1, height // 2 - 1), fill=RED)
    draw.rectangle((width // 2, 0, width - 1, height // 2 - 1), fill=GREEN)
    draw.rectangle((0, height // 2, width // 2 - 1, height - 1), fill=BLUE)
    draw.rectangle((width // 2, height // 2, width - 1, height - 1), fill=YELLOW)
    return image


def encode(image: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def gps_exif(orientation: int = 1) -> Image.Exif:
    """EXIF с моделью устройства и геометкой: по ним видно, что обработка их убрала."""
    exif = Image.Exif()
    exif[0x010F] = "SecretMaker"
    exif[0x0110] = "SecretModel"
    exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (55.0, 45.0, 21.0), 3: "E", 4: (37.0, 37.0, 4.0)}
    return exif


# Маленькие настоящие файлы: проходят и проверку сигнатуры, и разбор Pillow.
JPEG = encode(quadrants(), "JPEG", quality=85)
PNG = encode(quadrants(), "PNG")
GIF = encode(quadrants(), "GIF")
WEBP = encode(quadrants(), "WEBP", quality=80)
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
CONTENT_TYPES = {
    b"\xff\xd8": "image/jpeg",
    b"\x89P": "image/png",
    b"GI": "image/gif",
    b"RI": "image/webp",
}


def content_type_of(body: bytes) -> str:
    """Заявленный тип по первым байтам тестового файла (для обычных картинок)."""
    return CONTENT_TYPES.get(body[:2], "application/octet-stream")


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


async def read_urls(
    client: httpx.AsyncClient, user: SignedInUser, asset_id: str | uuid.UUID
) -> httpx.Response:
    return await client.get(f"{MEDIA}/{asset_id}/urls", headers=user.headers)


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


async def process(
    asset_id: str | uuid.UUID,
    sessions: Sessions,
    storage: InMemoryObjectStorage,
    jobs: InMemoryJobQueue,
    *,
    budget: DecodeBudget | None = None,
) -> Outcome:
    """Воркер media берёт задачу `process_media` для ресурса."""
    return await process_media(
        ProcessMedia(uuid.UUID(str(asset_id))),
        sessionmaker=sessions,
        storage=storage,
        jobs=jobs,
        budget=budget,
    )


async def ready(
    client: httpx.AsyncClient,
    user: SignedInUser,
    storage: InMemoryObjectStorage,
    jobs: InMemoryJobQueue,
    sessions: Sessions,
    body: bytes = JPEG,
    **overrides: Any,
) -> str:
    """Загрузка, завершение и обработка: ресурс `ready`. Возвращает его идентификатор."""
    overrides.setdefault("content_type", content_type_of(body))
    created = await uploaded(client, user, storage, body, **overrides)
    asset_id = str(created["asset"]["id"])
    outcome = await process(asset_id, sessions, storage, jobs)
    assert outcome is Outcome.READY, outcome
    return asset_id


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

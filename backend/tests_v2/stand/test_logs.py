"""Журнал доступа Caddy не содержит токенов, cookie и подписей presigned URL (⚖️ 4.15)."""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .conftest import Stand
from .sigv4 import presign_url

TAIL_BYTES = 4 * 1024 * 1024
"""Журнал доступа вырастает до 100 МиБ: тесту нужны свежие записи, весь файл читать незачем."""


def log_text(path: Path) -> str:
    """Конец журнала (последние `TAIL_BYTES`), без оборванной первой строки."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text if size <= TAIL_BYTES else text.partition(chr(10))[2]


async def entry_with(path: Path, needle: str, patience: float = 10.0) -> dict[str, Any]:
    """Ждёт строку журнала с `needle` (Caddy пишет с небольшой задержкой) и разбирает её."""
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        for line in log_text(path).splitlines():
            if needle in line:
                return json.loads(line)
        await asyncio.sleep(0.2)
    raise AssertionError(f"в журнале {path} нет записи с {needle!r}")


async def test_tokens_cookies_and_tickets_never_reach_the_access_log(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    marker = uuid.uuid4().hex[:12]
    await client.get(
        f"/api/v1/meta?ticket=ticket-{marker}&visible={marker}",
        headers={"Authorization": f"Bearer bearer-{marker}", "Cookie": f"refresh=cookie-{marker}"},
    )
    entry = await entry_with(stand.access_log, f"visible={marker}")

    assert "ticket=REDACTED" in entry["request"]["uri"]
    assert f"visible={marker}" in entry["request"]["uri"]  # остальные параметры остаются
    headers = {name.lower() for name in entry["request"]["headers"]}
    assert "authorization" not in headers
    assert "cookie" not in headers
    text = log_text(stand.access_log)
    for secret in (f"ticket-{marker}", f"bearer-{marker}", f"cookie-{marker}"):
        assert secret not in text


async def test_presigned_signatures_never_reach_the_access_log(
    client: httpx.AsyncClient, stand: Stand
) -> None:
    key = f"uploads/{uuid.uuid4().hex}/log-check.webp"
    url = presign_url(
        "GET",
        f"{stand.base_url}/media/{key}",
        access_key=stand.s3_access_key,
        secret_key=stand.s3_secret_key,
    )
    signature = url.split("X-Amz-Signature=")[1]
    await client.get(url)

    entry = await entry_with(stand.access_log, key)
    assert "X-Amz-Signature=REDACTED" in entry["request"]["uri"]
    assert "X-Amz-Credential=REDACTED" in entry["request"]["uri"]
    text = log_text(stand.access_log)
    assert signature not in text
    assert stand.s3_access_key not in text

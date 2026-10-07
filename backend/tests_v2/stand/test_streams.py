"""Спайк S4-02: SSE и WebSocket через Caddy на тестовых ручках (SPIKE_ENDPOINTS_ENABLED)."""

import asyncio
import json
import time
from itertools import pairwise

import httpx
import pytest
from websockets.asyncio.client import connect

from .conftest import Stand


async def tick_arrivals(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> tuple[list[float], httpx.Headers]:
    started = time.monotonic()
    async with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        arrivals = [
            time.monotonic() - started
            async for line in response.aiter_lines()
            if line.startswith("event: tick")
        ]
        return arrivals, response.headers


@pytest.mark.parametrize("accept_encoding", ["identity", "gzip, deflate, br, zstd"])
async def test_sse_is_not_buffered_by_caddy(
    client: httpx.AsyncClient, accept_encoding: str
) -> None:
    arrivals, headers = await tick_arrivals(
        client, "/api/v1/_spike/sse?count=4&interval=0.5", {"Accept-Encoding": accept_encoding}
    )

    assert len(arrivals) == 4
    assert arrivals[0] < 0.4, arrivals  # первое событие сразу, а не после конца потока
    gaps = [later - earlier for earlier, later in pairwise(arrivals)]
    assert all(gap > 0.3 for gap in gaps), arrivals
    assert headers["cache-control"] == "no-store"
    assert headers["x-accel-buffering"] == "no"


async def test_sse_comment_pulses_arrive_between_slow_events(client: httpx.AsyncClient) -> None:
    started = time.monotonic()
    url = "/api/v1/_spike/sse?count=2&interval=2&keepalive=0.5"
    async with client.stream("GET", url) as response:
        pulses = [
            time.monotonic() - started
            async for line in response.aiter_lines()
            if line.startswith(": keep-alive")
        ]
    assert len(pulses) >= 2
    assert pulses[0] < 1.0


async def test_websocket_echo_works_through_caddy(stand: Stand) -> None:
    async with connect(f"{stand.ws_url}/api/v1/_spike/ws", ssl=stand.ssl_context) as ws:
        await ws.send("привет")
        assert await ws.recv() == "привет"
        await ws.send(b"\x00\x01\x02")
        assert await ws.recv() == b"\x00\x01\x02"


async def test_websocket_survives_idle_periods_with_server_ticks(stand: Stand) -> None:
    """Caddy не обрывает простаивающее соединение (8 с; STAND_IDLE_SECONDS=90 для долгой проверки)."""
    async with connect(f"{stand.ws_url}/api/v1/_spike/ws?tick=1", ssl=stand.ssl_context) as ws:
        ticks = 0
        deadline = time.monotonic() + stand.idle_seconds
        while time.monotonic() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert message["type"] == "tick"
            ticks += 1
        assert ticks >= stand.idle_seconds - 2
        await ws.send("ещё здесь")
        while (message := await asyncio.wait_for(ws.recv(), timeout=3)) != "ещё здесь":
            assert json.loads(str(message))["type"] == "tick"

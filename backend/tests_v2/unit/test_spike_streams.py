"""Тестовые ручки SSE и WebSocket для спайка S4-02 на настоящем uvicorn: поток, пульс, слив.

Сервер поднимается в процессе теста на свободном порту (без базы и Redis): так проверяются и
сокеты, и порядок остановки `GracefulServer`, а не только обработчики.
"""

import asyncio
import json
import signal
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from messunjerr.core.server import GracefulServer
from messunjerr.core.shutdown import ShutdownGate
from messunjerr.spike import WS_CLOSE_GOING_AWAY, spike_router


@dataclass(frozen=True, slots=True)
class LiveServer:
    port: int
    gate: ShutdownGate
    server: GracefulServer
    task: "asyncio.Task[None]"

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


@pytest_asyncio.fixture
async def live(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[LiveServer]:
    gate = ShutdownGate()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        gate.attach()
        yield
        gate.detach()

    app = FastAPI(lifespan=lifespan)
    app.state.shutdown = gate
    app.include_router(spike_router)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, timeout_graceful_shutdown=2
    )
    server = GracefulServer(config, gate=gate, drain_seconds=0.2)
    # Настоящие обработчики сигналов подменили бы сигналы самого pytest и в конце подняли бы
    # SIGTERM в тестовом процессе, поэтому слив запускается прямым вызовом `handle_exit`.
    monkeypatch.setattr(server, "capture_signals", nullcontext)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 (у uvicorn.Server только флаг, события нет)
        await asyncio.sleep(0.01)
    port: int = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield LiveServer(port=port, gate=gate, server=server, task=task)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


async def read_events(lines: AsyncIterator[str]) -> list[dict[str, str]]:
    """Разбирает остаток потока; итератор общий, чтобы можно было прочитать часть и продолжить."""
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    async for line in lines:
        if line == "":
            if current:
                events.append(current)
                current = {}
        elif line.startswith(":"):
            events.append({"comment": line[1:].strip()})
        else:
            key, _, value = line.partition(": ")
            current[key] = value
    return events


# ----------------------------------------------------------------------------- SSE
async def test_sse_streams_the_requested_ticks_and_ends(live: LiveServer) -> None:
    async with (
        httpx.AsyncClient() as client,
        client.stream("GET", f"{live.http}/api/v1/_spike/sse?count=3&interval=0.05") as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        events = await read_events(response.aiter_lines())

    ticks = [event for event in events if event.get("event") == "tick"]
    assert [event["id"] for event in ticks] == ["1", "2", "3"]
    assert json.loads(ticks[0]["data"])["n"] == 1
    assert events[-1] == {"event": "end", "data": "done"}


async def test_sse_events_arrive_one_by_one_and_not_all_at_the_end(live: LiveServer) -> None:
    started = time.monotonic()
    async with (
        httpx.AsyncClient() as client,
        client.stream("GET", f"{live.http}/api/v1/_spike/sse?count=3&interval=0.3") as response,
    ):
        arrivals = [
            time.monotonic() - started
            async for line in response.aiter_lines()
            if line.startswith("event: tick")
        ]

    assert len(arrivals) == 3
    assert arrivals[0] < 0.25  # первое событие сразу, не после конца потока
    assert arrivals[1] - arrivals[0] > 0.2
    assert arrivals[2] - arrivals[1] > 0.2


async def test_sse_pulse_is_sent_while_waiting_for_the_next_tick(live: LiveServer) -> None:
    url = f"{live.http}/api/v1/_spike/sse?count=2&interval=0.5&keepalive=0.1"
    async with httpx.AsyncClient() as client, client.stream("GET", url) as response:
        events = await read_events(response.aiter_lines())
    assert any(event.get("comment") == "keep-alive" for event in events)


async def test_sse_says_bye_and_ends_when_the_process_starts_to_drain(live: LiveServer) -> None:
    url = f"{live.http}/api/v1/_spike/sse?count=1000&interval=0.05"
    async with httpx.AsyncClient() as client, client.stream("GET", url) as response:
        lines = response.aiter_lines()
        async for line in lines:
            if line.startswith("event: tick"):
                break
        live.gate.begin()
        events = await read_events(lines)

    assert events[-1] == {"event": "bye", "data": "draining"}


async def test_sse_rejects_absurd_parameters(live: LiveServer) -> None:
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{live.http}/api/v1/_spike/sse?count=0")).status_code == 422
        assert (await client.get(f"{live.http}/api/v1/_spike/sse?interval=0")).status_code == 422


# ----------------------------------------------------------------------------- WebSocket
async def test_ws_echoes_text_and_bytes(live: LiveServer) -> None:
    async with connect(f"{live.ws}/api/v1/_spike/ws") as ws:
        await ws.send("привет")
        assert await ws.recv() == "привет"
        await ws.send(b"\x00\x01\x02")
        assert await ws.recv() == b"\x00\x01\x02"


async def test_ws_sends_ticks_when_asked(live: LiveServer) -> None:
    async with connect(f"{live.ws}/api/v1/_spike/ws?tick=0.05") as ws:
        first = json.loads(await ws.recv())
        second = json.loads(await ws.recv())
    assert first["type"] == second["type"] == "tick"
    assert second["at"] >= first["at"]


async def test_ws_ticks_do_not_break_the_echo(live: LiveServer) -> None:
    async with connect(f"{live.ws}/api/v1/_spike/ws?tick=0.05") as ws:
        await ws.send("ping")
        seen: list[str] = []
        while (message := await ws.recv()) != "ping":
            assert isinstance(message, str)
            seen.append(message)
    assert all(json.loads(item)["type"] == "tick" for item in seen)


async def test_ws_closes_with_going_away_when_the_process_starts_to_drain(
    live: LiveServer,
) -> None:
    async with connect(f"{live.ws}/api/v1/_spike/ws") as ws:
        await ws.send("hello")
        assert await ws.recv() == "hello"
        live.gate.begin()
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=2)

    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == WS_CLOSE_GOING_AWAY == 1001


# ----------------------------------------------------------------------------- GracefulServer вживую
async def test_graceful_server_drains_the_stream_then_stops_listening(live: LiveServer) -> None:
    url = f"{live.http}/api/v1/_spike/sse?count=1000&interval=0.05"
    async with httpx.AsyncClient() as client, client.stream("GET", url) as response:
        lines = response.aiter_lines()
        async for line in lines:
            if line.startswith("event: tick"):
                break
        live.server.handle_exit(signal.SIGTERM, None)
        assert live.gate.draining is True
        assert live.server.should_exit is False  # пауза слива: реплика ещё живая
        events = await read_events(lines)

    assert events[-1] == {"event": "bye", "data": "draining"}
    await asyncio.wait_for(live.task, timeout=5)  # после паузы uvicorn закрылся сам
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", live.port)

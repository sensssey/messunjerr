"""🔬 Тестовые ручки SSE и WebSocket для спайка через Caddy (S4-02). Убираются в S10.

Нужны, чтобы проверить на стенде, что балансировщик не буферизует потоки, не обрывает простаивающие
соединения и что слив при выкладке работает: поток завершается событием `bye`, WebSocket закрывается
кодом 1001, клиент переподключается к соседней реплике. Включаются `SPIKE_ENDPOINTS_ENABLED=true`
(в prod запрещено), без авторизации, в OpenAPI не попадают.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.types import Message

from messunjerr.core.shutdown import ShutdownGate

spike_router = APIRouter(prefix="/api/v1/_spike", tags=["spike"], include_in_schema=False)

WS_CLOSE_GOING_AWAY = 1001  # «сервер уходит»: клиенту нужно переподключиться (4.9)


def _event(name: str, data: str, *, event_id: int | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {data}\n\n".encode()


async def _sse_stream(
    gate: ShutdownGate, *, count: int, interval: float, keepalive: float
) -> AsyncIterator[bytes]:
    yield b"retry: 3000\n\n"
    sent = 0
    next_tick = time.monotonic()
    last_write = time.monotonic()
    while sent < count:
        deadline = min(next_tick, last_write + keepalive)
        try:
            await asyncio.wait_for(gate.wait(), timeout=max(deadline - time.monotonic(), 0))
        except TimeoutError:
            pass
        else:
            yield _event("bye", "draining")
            return
        now = time.monotonic()
        if now >= next_tick:
            sent += 1
            payload = json.dumps({"n": sent, "sent_at": time.time()})
            yield _event("tick", payload, event_id=sent)
            next_tick += interval
            last_write = now
        elif now - last_write >= keepalive:
            yield b": keep-alive\n\n"
            last_write = now
    yield _event("end", "done")


@spike_router.get("/sse")
async def spike_sse(
    request: Request,
    count: Annotated[int, Query(ge=1, le=3600)] = 5,
    interval: Annotated[float, Query(ge=0.05, le=60)] = 1.0,
    keepalive: Annotated[float, Query(ge=0.1, le=60)] = 20.0,
) -> StreamingResponse:
    """`count` событий `tick` с шагом `interval` секунд; между ними комментарий-пульс."""
    gate: ShutdownGate = request.app.state.shutdown
    return StreamingResponse(
        _sse_stream(gate, count=count, interval=interval, keepalive=keepalive),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@spike_router.websocket("/ws")
async def spike_ws(websocket: WebSocket, tick: Annotated[float, Query(ge=0, le=60)] = 0.0) -> None:
    """Эхо текста и байтов; при `tick` > 0 ещё присылает `{"type": "tick"}` каждые `tick` секунд."""
    gate: ShutdownGate = websocket.app.state.shutdown
    await websocket.accept()
    drain = asyncio.create_task(gate.wait())
    receive: asyncio.Task[Message] = asyncio.create_task(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {receive, drain}, timeout=tick or None, return_when=asyncio.FIRST_COMPLETED
            )
            if drain in done:
                await websocket.close(code=WS_CLOSE_GOING_AWAY, reason="server restarting")
                return
            if receive not in done:
                await websocket.send_json({"type": "tick", "at": time.time()})
                continue
            message = receive.result()
            if message["type"] == "websocket.disconnect":
                return
            text: str | None = message.get("text")
            data: bytes | None = message.get("bytes")
            if text is not None:
                await websocket.send_text(text)
            elif data is not None:
                await websocket.send_bytes(data)
            receive = asyncio.create_task(websocket.receive())
    except WebSocketDisconnect:
        return
    finally:
        drain.cancel()
        receive.cancel()

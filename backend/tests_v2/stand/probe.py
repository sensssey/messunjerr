"""Фоновая нагрузка на время выкладки (S4-03): клиенты не должны видеть ошибок.

Это не тест, а программа: deploy/rollout.sh запускает её отдельным контейнером stand-tools на
время выкладки и останавливает по SIGTERM, после чего она печатает итог и завершается кодом 0, если
всё прошло чисто. Что она делает через Caddy:

- несколько циклов обычных запросов `GET /api/v1/meta` и `GET /.well-known/jwks.json`: любой ответ
  не 200 и любое исключение это ошибка; по `build` из /meta видно, какие версии отвечали;
- один SSE-поток с переподключением: при сливе реплики он должен закончиться событием `bye`;
- одно WebSocket-соединение с переподключением: при сливе реплики оно должно закрыться кодом 1001.

«Резкий» обрыв потока без `bye`, закрытие WebSocket другим кодом, любая ошибка подключения и
неожиданное исключение в самой пробе считаются сбоем выкладки. Нагрузка, которая ничего не
открыла (нет ни потока SSE, ни WebSocket, ни запросов), тоже сбой: молчание не доказательство.
"""

import asyncio
import os
import signal
import ssl
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

BASE_URL = os.environ["STAND_BASE_URL"].rstrip("/")
CA_FILE = os.environ["STAND_CA_FILE"]
HTTP_WORKERS = int(os.environ.get("PROBE_HTTP_WORKERS", "6"))
GOING_AWAY = 1001

http_status: Counter[str] = Counter()
http_builds: Counter[str] = Counter()
http_problems: list[str] = []
sse: Counter[str] = Counter()
ws: Counter[str] = Counter()
crashes: Counter[str] = Counter()
started_at = time.monotonic()


def note_problem(text: str) -> None:
    if len(http_problems) < 10:
        http_problems.append(f"+{time.monotonic() - started_at:.1f}s {text}")


async def http_worker(client: httpx.AsyncClient, stop: asyncio.Event, path: str) -> None:
    while not stop.is_set():
        try:
            response = await client.get(path)
        except httpx.HTTPError as error:
            http_status[type(error).__name__] += 1
            note_problem(f"GET {path}: {type(error).__name__} {error}")
        else:
            http_status[str(response.status_code)] += 1
            if response.status_code != 200:
                note_problem(f"GET {path}: {response.status_code}")
            elif path.endswith("/meta"):
                http_builds[response.json()["build"]] += 1
        await asyncio.sleep(0.02)


async def sse_worker(client: httpx.AsyncClient, stop: asyncio.Event) -> None:
    while not stop.is_set():
        ending = ""
        try:
            async with client.stream("GET", "/api/v1/_spike/sse?count=3600&interval=1") as response:
                sse["opened"] += 1
                async for line in response.aiter_lines():
                    if line.startswith("event: bye"):
                        ending = "bye"
        except httpx.HTTPError as error:
            if not stop.is_set():
                sse["abrupt"] += 1
                note_problem(f"SSE: {type(error).__name__} {error}")
        else:
            sse["graceful" if ending == "bye" else "abrupt"] += 1
            if ending != "bye":
                note_problem("SSE: поток закончился без bye")
        await asyncio.sleep(0.5)


async def ws_worker(stop: asyncio.Event, context: ssl.SSLContext) -> None:
    url = BASE_URL.replace("https://", "wss://", 1) + "/api/v1/_spike/ws?tick=1"
    while not stop.is_set():
        try:
            async with connect(url, ssl=context, open_timeout=5) as socket:
                ws["opened"] += 1
                try:
                    while True:
                        await socket.recv()
                except ConnectionClosed as closed:
                    code = closed.rcvd.code if closed.rcvd else 1006
                    ws[f"closed_{code}"] += 1
                    if code != GOING_AWAY:
                        note_problem(f"WS: закрыт кодом {code}")
        except (OSError, TimeoutError, WebSocketException) as error:
            # 502 или 503 при рукопожатии приходят как InvalidStatus, а не как OSError.
            ws["connect_errors"] += 1
            note_problem(f"WS: не подключился: {type(error).__name__} {error}")
        await asyncio.sleep(0.5)


async def supervised(name: str, worker: Callable[[], Awaitable[None]], stop: asyncio.Event) -> None:
    """Неожиданное исключение в цикле не должно тихо убить его: это ошибка самой выкладки или пробы."""
    while not stop.is_set():
        try:
            await worker()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            crashes[name] += 1
            note_problem(f"{name}: исключение {type(error).__name__} {error}")
            await asyncio.sleep(0.5)


def verdict() -> tuple[bool, list[str]]:
    requests = sum(http_status.values())
    errors = requests - http_status["200"]
    abrupt_ws = sum(
        count
        for name, count in ws.items()
        if name.startswith("closed_") and name != f"closed_{GOING_AWAY}"
    )
    crashed = sum(crashes.values())
    lines = [
        f"http: запросов {requests}, ошибок {errors}, статусы {dict(http_status)}",
        f"http: версии, которые отвечали (build): {dict(http_builds)}",
        f"sse: открыто {sse['opened']}, закончено сливом (bye) {sse['graceful']}, оборвано {sse['abrupt']}",
        (
            f"ws: открыто {ws['opened']}, закрыто кодом 1001 {ws['closed_1001']}, "
            f"закрыто другим кодом {abrupt_ws}, ошибок подключения {ws['connect_errors']}"
        ),
        f"проба: неожиданных исключений {crashed}",
    ]
    lines += [f"проблема: {problem}" for problem in http_problems]
    ok = (
        requests > 0
        and sse["opened"] > 0
        and ws["opened"] > 0
        and errors == 0
        and sse["abrupt"] == 0
        and abrupt_ws == 0
        and ws["connect_errors"] == 0
        and crashed == 0
    )
    return ok, lines


async def main() -> int:
    context = ssl.create_default_context(cafile=CA_FILE)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    async with httpx.AsyncClient(base_url=BASE_URL, verify=context, timeout=15) as client:

        def http_job(path: str) -> Callable[[], Awaitable[None]]:
            return lambda: http_worker(client, stop, path)

        tasks = [
            asyncio.create_task(
                supervised(
                    f"http-{index}",
                    http_job("/api/v1/meta" if index % 3 else "/.well-known/jwks.json"),
                    stop,
                )
            )
            for index in range(HTTP_WORKERS)
        ]
        tasks.append(asyncio.create_task(supervised("sse", lambda: sse_worker(client, stop), stop)))
        tasks.append(asyncio.create_task(supervised("ws", lambda: ws_worker(stop, context), stop)))
        print("probe: нагрузка идёт, остановка по SIGTERM", flush=True)
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    ok, lines = verdict()
    print("\n".join(lines))
    print("probe: ОШИБОК НЕТ" if ok else "probe: ЕСТЬ ОШИБКИ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

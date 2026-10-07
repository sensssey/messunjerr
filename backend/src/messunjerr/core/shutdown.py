"""Мягкая остановка процесса (S4-03): сначала слив трафика, потом закрытие.

Выкладка без простоя держится на порядке действий при SIGTERM:

1. `ShutdownGate.begin()`: `/health/serving` и `/health/ready` отвечают 503, Caddy по активной
   проверке `/health/serving` снимает реплику с балансировки, долгоживущие соединения (WebSocket,
   SSE) сами закрываются с кодом 1001 и клиенты переподключаются к соседней реплике;
2. спустя `SHUTDOWN_DRAIN_SECONDS` uvicorn перестаёт принимать новые соединения и дожидается
   текущих запросов (до `SHUTDOWN_TIMEOUT_SECONDS`), затем lifespan освобождает ресурсы.

Калитка одна на процесс: сигнал приходит в `GracefulServer` (core/server.py), а читают её
приложение и обработчики потоков. Асинхронная часть привязывается к циклу, который обслуживает
запросы (`attach` в lifespan), поэтому `begin` можно вызывать из обработчика сигнала.
"""

import asyncio
from functools import lru_cache


class ShutdownGate:
    def __init__(self) -> None:
        self._draining = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    @property
    def draining(self) -> bool:
        """Процесс закрывается: новую работу брать не нужно."""
        return self._draining

    def attach(self) -> None:
        """Привязывает калитку к текущему циклу событий; вызывается из lifespan."""
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        if self._draining:
            self._event.set()

    def detach(self) -> None:
        self._loop = None
        self._event = None

    def begin(self) -> None:
        """Объявляет слив. Безопасна из любого потока, в том числе из обработчика сигнала."""
        self._draining = True
        loop, event = self._loop, self._event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # цикл уже закрыт: ждать нечего
            return

    async def wait(self) -> None:
        """Возвращается, когда начат слив. Нужна привязка (`attach`), иначе ошибка."""
        if self._event is None:
            raise RuntimeError("ShutdownGate не привязана к циклу событий (attach в lifespan)")
        await self._event.wait()

    def reset(self) -> None:
        """Сбрасывает состояние: для тестов, которые делят процесс."""
        self._draining = False
        self._loop = None
        self._event = None


@lru_cache(maxsize=1)
def get_shutdown_gate() -> ShutdownGate:
    """Калитка процесса: её видят `create_app` (по фабрике uvicorn) и `GracefulServer`."""
    return ShutdownGate()

"""Запуск API в uvicorn с мягкой остановкой (S4-03).

`GracefulServer` отличается от обычного `uvicorn.Server` одним: по первому SIGTERM или SIGINT он
не останавливается сразу, а сначала объявляет слив (`ShutdownGate`) и ждёт `drain_seconds`, чтобы
балансировщик успел снять реплику. Повторный сигнал останавливает как обычно: оператор не ждёт.
"""

import os
import threading
from types import FrameType

import uvicorn

from messunjerr.core.shutdown import ShutdownGate, get_shutdown_gate
from messunjerr.settings import Settings


class GracefulServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, gate: ShutdownGate, drain_seconds: float) -> None:
        super().__init__(config)
        self._gate = gate
        self._drain_seconds = drain_seconds
        self._drain_started = False
        self._timer: threading.Timer | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        first = not self._drain_started
        self._drain_started = True
        self._gate.begin()
        if first and self._drain_seconds > 0:
            # Обработчик сигнала не должен спать: цикл событий стоит в том же потоке.
            self._timer = threading.Timer(
                self._drain_seconds, super().handle_exit, args=(sig, frame)
            )
            self._timer.daemon = True
            self._timer.start()
            return
        if self._timer is not None:
            # Повторный сигнал пропускает паузу; забытый таймер не должен потом ещё раз «добить»
            # остановку (второй SIGINT от таймера включил бы force_exit).
            self._timer.cancel()
            self._timer = None
        super().handle_exit(sig, frame)


def run_server(settings: Settings, *, host: str, port: int) -> None:
    """Один процесс uvicorn: в проде реплики масштабируются контейнерами, а не `--workers`."""
    config = uvicorn.Config(
        "messunjerr.main:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )
    server = GracefulServer(
        config, gate=get_shutdown_gate(), drain_seconds=settings.shutdown_drain_seconds
    )
    server.run()

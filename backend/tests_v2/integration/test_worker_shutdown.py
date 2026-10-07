"""Остановка воркера по SIGTERM (так Docker останавливает контейнер при выкладке).

`Worker.async_run` от arq при сигнале отменяет главную задачу и сама ничего не закрывает: без обработки в
`run_worker` остановка заканчивалась трассировкой `CancelledError`, кодом 1, незакрытым клиентом S3
(«Unclosed client session») и пропущенным `on_shutdown`. Тест запускает воркер настоящим процессом.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from typing import IO

import pytest

from messunjerr.settings import Settings

STARTUP_SECONDS = 40
STOP_SECONDS = 30


def _wait_for(log: IO[str], text: str, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    content = ""
    while time.monotonic() < deadline:
        log.seek(0)
        content = log.read()
        if text in content:
            return content
        time.sleep(0.3)
    return content


@pytest.mark.parametrize("queue", ["default", "media"])
def test_a_worker_stops_cleanly_on_sigterm(test_settings: Settings, queue: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": test_settings.database_url.get_secret_value(),
        "REDIS_URL": test_settings.redis_url.get_secret_value(),
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
    }
    # Хранилище не нужно ни одному из них для запуска: клиент S3 создаётся лениво, при первом обращении.
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(  # noqa: S603 (запускаем собственный модуль)
            [sys.executable, "-m", "messunjerr", "worker", "--queue", queue],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            started = _wait_for(log, "worker_started", STARTUP_SECONDS)
            assert "worker_started" in started, started
            process.send_signal(signal.SIGTERM)
            try:
                code = process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                pytest.fail("воркер не остановился за 30 секунд после SIGTERM")
            log.seek(0)
            output = log.read()
        finally:
            if process.poll() is None:
                process.kill()

    assert "Traceback" not in output, output
    assert "CancelledError" not in output, output
    assert "worker_stopped" in output, (
        output
    )  # on_shutdown выполнен: пул БД, очередь и клиент закрыты
    assert code == 0, output

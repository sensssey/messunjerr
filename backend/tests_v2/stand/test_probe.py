"""Фоновая нагрузка выкладки (probe.py) сама проверена: молчание и сбои пробы не дают «ошибок нет»."""

import asyncio
import importlib.util
import ssl
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from websockets.exceptions import InvalidHandshake

pytestmark = pytest.mark.offline


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Свежий экземпляр модуля на каждый тест: счётчики лежат на уровне модуля."""
    monkeypatch.setenv("STAND_BASE_URL", "https://stand.invalid")
    monkeypatch.setenv("STAND_CA_FILE", "/nonexistent/root.crt")
    spec = importlib.util.spec_from_file_location(
        "probe_under_test", Path(__file__).with_name("probe.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_run(probe: Any) -> None:
    probe.http_status["200"] = 100
    probe.sse.update({"opened": 3, "graceful": 2})
    probe.ws.update({"opened": 3, "closed_1001": 2})


def test_a_clean_run_passes(probe: Any) -> None:
    clean_run(probe)
    ok, lines = probe.verdict()
    assert ok, lines


def test_silence_is_not_success(probe: Any) -> None:
    ok, _ = probe.verdict()
    assert ok is False  # ни одного запроса


@pytest.mark.parametrize("silent", ["sse", "ws"])
def test_a_stream_that_never_opened_is_a_failure(probe: Any, silent: str) -> None:
    clean_run(probe)
    getattr(probe, silent)["opened"] = 0
    ok, _ = probe.verdict()
    assert ok is False


@pytest.mark.parametrize(
    ("counter", "key"),
    [
        ("http_status", "503"),
        ("http_status", "ReadTimeout"),
        ("sse", "abrupt"),
        ("ws", "closed_1006"),
        ("ws", "closed_1012"),
        ("ws", "connect_errors"),
        ("crashes", "http-0"),
    ],
)
def test_every_kind_of_client_visible_failure_fails_the_run(
    probe: Any, counter: str, key: str
) -> None:
    clean_run(probe)
    getattr(probe, counter)[key] += 1
    ok, _ = probe.verdict()
    assert ok is False


async def test_a_handshake_refused_by_the_proxy_counts_as_a_connect_error(
    probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 502 или 503 на рукопожатии приходят из websockets как InvalidStatus (не OSError).
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise InvalidHandshake("502 от прокси")

    monkeypatch.setattr(probe, "connect", refuse)
    stop = asyncio.Event()
    worker = asyncio.create_task(probe.ws_worker(stop, ssl.create_default_context()))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(worker, timeout=2)

    assert probe.ws["connect_errors"] >= 1
    clean_run(probe)
    ok, _ = probe.verdict()
    assert ok is False


async def test_an_unexpected_exception_in_a_worker_is_recorded_not_swallowed(probe: Any) -> None:
    stop = asyncio.Event()
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("сломалось")
        await stop.wait()

    task = asyncio.create_task(probe.supervised("flaky", flaky, stop))
    await asyncio.sleep(0.8)  # одна пауза после сбоя 0,5 с и повторный запуск
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert probe.crashes["flaky"] == 1
    assert calls == 2  # цикл перезапущен, а не умер молча
    assert any("сломалось" in problem for problem in probe.http_problems)

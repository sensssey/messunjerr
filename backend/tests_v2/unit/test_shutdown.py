"""Мягкая остановка (S4-03): калитка слива, сервер с паузой перед закрытием, настройки."""

import asyncio
import signal
import threading
import time

import pytest
import uvicorn
from pydantic import SecretStr

from messunjerr.core.server import GracefulServer
from messunjerr.core.shutdown import ShutdownGate, get_shutdown_gate
from messunjerr.settings import Settings, check_runtime


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "database_url": SecretStr("postgresql+asyncpg://app:x@db/messunjerr"),
        "redis_url": SecretStr("redis://:x@redis:6379/0"),
    }
    return Settings.model_validate({**base, **overrides})


# ----------------------------------------------------------------------------- ShutdownGate
async def test_gate_starts_open_and_begin_closes_it() -> None:
    gate = ShutdownGate()
    gate.attach()
    assert gate.draining is False
    gate.begin()
    assert gate.draining is True
    await asyncio.wait_for(gate.wait(), timeout=1)


async def test_gate_wakes_waiters_when_begin_comes_from_another_thread() -> None:
    gate = ShutdownGate()
    gate.attach()
    waiter = asyncio.create_task(gate.wait())
    await asyncio.sleep(0)
    assert not waiter.done()

    threading.Thread(target=gate.begin).start()
    await asyncio.wait_for(waiter, timeout=1)


async def test_gate_begun_before_attach_is_already_open_for_waiters() -> None:
    gate = ShutdownGate()
    gate.begin()
    gate.attach()
    await asyncio.wait_for(gate.wait(), timeout=1)


async def test_waiting_without_attach_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="attach"):
        await ShutdownGate().wait()


async def test_gate_ignores_begin_after_its_loop_is_gone() -> None:
    gate = ShutdownGate()
    gate.attach()
    gate.detach()
    gate.begin()  # не падает: цикл уже закрыт, остаётся только признак
    assert gate.draining is True


def test_begin_does_not_fail_when_the_loop_has_already_closed() -> None:
    gate = ShutdownGate()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_attach(gate))
    loop.close()
    gate.begin()
    assert gate.draining is True


async def _attach(gate: ShutdownGate) -> None:
    gate.attach()


def test_reset_returns_the_gate_to_the_open_state() -> None:
    gate = ShutdownGate()
    gate.begin()
    gate.reset()
    assert gate.draining is False


def test_process_gate_is_a_singleton() -> None:
    assert get_shutdown_gate() is get_shutdown_gate()


# ----------------------------------------------------------------------------- GracefulServer
def make_server(gate: ShutdownGate, drain_seconds: float) -> GracefulServer:
    config = uvicorn.Config("messunjerr.main:create_app", factory=True)
    return GracefulServer(config, gate=gate, drain_seconds=drain_seconds)


def wait_until(condition: object, *, timeout: float = 2.0) -> bool:
    assert callable(condition)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_signal_without_drain_stops_at_once() -> None:
    gate = ShutdownGate()
    server = make_server(gate, drain_seconds=0)
    server.handle_exit(signal.SIGTERM, None)
    assert gate.draining is True
    assert server.should_exit is True


def test_signal_with_drain_closes_the_gate_first_and_stops_later() -> None:
    gate = ShutdownGate()
    server = make_server(gate, drain_seconds=0.3)
    server.handle_exit(signal.SIGTERM, None)

    assert gate.draining is True
    assert server.should_exit is False  # реплика ещё принимает запросы, пока Caddy её снимает
    assert wait_until(lambda: server.should_exit)


def test_second_signal_skips_the_remaining_drain() -> None:
    gate = ShutdownGate()
    server = make_server(gate, drain_seconds=30)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is False

    server.handle_exit(signal.SIGTERM, None)  # оператор не ждёт 30 секунд
    assert server.should_exit is True


def test_second_signal_cancels_the_pending_timer() -> None:
    gate = ShutdownGate()
    server = make_server(gate, drain_seconds=0.3)
    server.handle_exit(signal.SIGINT, None)
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit is True

    time.sleep(0.6)  # без отмены таймер ещё раз вызвал бы handle_exit(SIGINT) и включил force_exit
    assert server.force_exit is False


# ----------------------------------------------------------------------------- настройки
def test_drain_is_off_by_default_and_bounded() -> None:
    settings = make_settings()
    assert settings.shutdown_drain_seconds == 0
    assert settings.shutdown_timeout_seconds == 20
    with pytest.raises(ValueError, match="shutdown_drain_seconds"):
        make_settings(shutdown_drain_seconds=-1)
    with pytest.raises(ValueError, match="shutdown_drain_seconds"):
        make_settings(shutdown_drain_seconds=61)


def test_spike_endpoints_are_off_by_default() -> None:
    assert make_settings().spike_endpoints_enabled is False


def test_prod_refuses_the_spike_endpoints() -> None:
    settings = make_settings(
        app_env="prod",
        public_base_url="https://example.ru",
        jwt_private_key=SecretStr("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"),
        spike_endpoints_enabled=True,
    )
    with pytest.raises(RuntimeError, match="SPIKE_ENDPOINTS_ENABLED"):
        check_runtime(settings)


def test_stage_allows_the_spike_endpoints_for_the_stand() -> None:
    settings = make_settings(
        app_env="stage",
        public_base_url="https://messunjerr.localhost",
        jwt_private_key=SecretStr("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"),
        spike_endpoints_enabled=True,
    )
    check_runtime(settings)

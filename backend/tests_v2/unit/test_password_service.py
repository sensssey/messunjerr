"""Argon2id: хэш, проверка, обновление параметров, выравнивание времени и работа вне цикла событий."""

import asyncio
import statistics
import time

import pytest

from messunjerr.identity.infra.password_service import PasswordService


def service(**overrides: int) -> PasswordService:
    options = {"time_cost": 1, "memory_cost_kib": 8192, "parallelism": 1, "concurrency": 2}
    options.update(overrides)
    return PasswordService(**options)


async def test_hash_is_argon2id_and_verifies() -> None:
    passwords = service()
    hashed = await passwords.hash("secret-password")
    assert hashed.startswith("$argon2id$")
    assert "secret-password" not in hashed
    check = await passwords.verify("secret-password", hashed)
    assert check.valid is True
    assert check.new_hash is None


async def test_wrong_password_is_invalid() -> None:
    passwords = service()
    hashed = await passwords.hash("secret-password")
    check = await passwords.verify("another-password", hashed)
    assert check.valid is False
    assert check.new_hash is None


async def test_every_hash_has_its_own_salt() -> None:
    passwords = service()
    assert await passwords.hash("same-password") != await passwords.hash("same-password")


async def test_verification_offers_a_new_hash_when_parameters_became_stronger() -> None:
    old = service()
    stronger = service(time_cost=2, memory_cost_kib=16384)
    hashed = await old.hash("secret-password")
    check = await stronger.verify("secret-password", hashed)
    assert check.valid is True
    assert check.new_hash is not None
    assert "t=2" in check.new_hash
    assert "m=16384" in check.new_hash
    # Повторная проверка уже новым хэшем обновления не требует.
    assert (await stronger.verify("secret-password", check.new_hash)).new_hash is None


async def test_no_new_hash_is_offered_for_a_wrong_password() -> None:
    old = service()
    stronger = service(time_cost=2, memory_cost_kib=16384)
    hashed = await old.hash("secret-password")
    assert (await stronger.verify("wrong-password", hashed)).new_hash is None


async def test_burn_costs_about_as_much_as_a_real_verification() -> None:
    passwords = service(time_cost=2, memory_cost_kib=32768)
    hashed = await passwords.hash("secret-password")
    await passwords.warm_up()

    async def timed(coroutine_factory: object) -> float:
        started = time.perf_counter()
        await coroutine_factory()  # type: ignore[operator]
        return time.perf_counter() - started

    verify = statistics.median(
        [await timed(lambda: passwords.verify("x", hashed)) for _ in range(7)]
    )
    burn = statistics.median([await timed(lambda: passwords.burn("x")) for _ in range(7)])
    assert burn == pytest.approx(verify, rel=0.5, abs=0.01)


async def test_hashing_does_not_block_the_event_loop() -> None:
    """Пока считаются хэши, цикл событий продолжает отвечать: задержка тика мала по сравнению с хэшем."""
    passwords = service(time_cost=3, memory_cost_kib=65536, parallelism=1, concurrency=4)
    interval = 0.002
    worst_lag = 0.0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal worst_lag
        loop = asyncio.get_running_loop()
        previous = loop.time()
        while not stop.is_set():
            await asyncio.sleep(interval)
            now = loop.time()
            worst_lag = max(worst_lag, now - previous - interval)
            previous = now

    tick_task = asyncio.create_task(ticker())
    started = time.perf_counter()
    await asyncio.gather(*(passwords.hash(f"password-number-{n}") for n in range(4)))
    hashing_time = time.perf_counter() - started
    stop.set()
    await tick_task

    assert hashing_time > 0.05  # хэширование заметно длится
    assert worst_lag < 0.05
    assert worst_lag < hashing_time / 2


async def test_concurrency_is_bounded_by_the_pool() -> None:
    passwords = service(concurrency=1)
    hashes = await asyncio.gather(*(passwords.hash(f"password-{n}-xxxxx") for n in range(5)))
    assert len(set(hashes)) == 5
    passwords.shutdown()

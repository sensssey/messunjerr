"""Сценарий S1 целиком, а также свойства, которые нельзя проверить одним запросом: время ответа и цикл событий."""

import asyncio
import statistics
import time
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.main import create_app
from messunjerr.settings import Settings

from .helpers import (
    PASSWORD,
    bearer,
    new_credentials,
    token_from_rendered_email,
    verified_user,
)

REGISTER = "/api/v1/auth/register"
VERIFY = "/api/v1/auth/verify-email"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/me"


async def test_registration_to_me_scenario(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """Демо S1: регистрация → письмо → подтверждение → вход → `GET /me`."""
    credentials = new_credentials()

    registered = await client.post(REGISTER, json=credentials)
    assert registered.status_code == 201

    early = await client.post(LOGIN, json={"login": credentials["email"], "password": PASSWORD})
    assert (early.status_code, early.json()["code"]) == (403, "email_not_verified")

    # Человек берёт токен из текста письма, собранного по шаблону.
    token = token_from_rendered_email(jobs, credentials["email"])
    verified = await client.post(VERIFY, json={"token": token})
    assert verified.status_code == 200

    me = await client.get(ME, headers=bearer(verified.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == credentials["username"]
    assert me.json()["status"] == "active"

    login = await client.post(LOGIN, json={"login": credentials["username"], "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["session_id"] != verified.json()["session_id"]
    again = await client.get(ME, headers=bearer(login.json()["access_token"]))
    assert again.json() == me.json()


# ----------------------------------------------------------------------------- тяжёлый Argon2id
@pytest_asyncio.fixture
async def heavy_client(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> AsyncIterator[httpx.AsyncClient]:
    """Приложение с настоящей стоимостью хэша: на дешёвом Argon2id время ответа определяет БД."""
    settings = test_settings.model_copy(
        update={
            "argon2_time_cost": 3,
            "argon2_memory_cost_kib": 65_536,
            "argon2_parallelism": 1,
            "password_hash_concurrency": 4,
        }
    )
    application = create_app(settings, job_queue=jobs)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def timed(
    client: httpx.AsyncClient, path: str, body: dict[str, object], status: int
) -> float:
    started = time.perf_counter()
    response = await client.post(path, json=body)
    elapsed = time.perf_counter() - started
    assert response.status_code == status, response.text
    return elapsed


async def test_registration_time_does_not_reveal_whether_the_email_exists(
    heavy_client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(heavy_client, jobs)
    fresh: list[float] = []
    taken: list[float] = []

    for _ in range(8):  # чередуем, чтобы дрейф нагрузки действовал на обе серии одинаково
        fresh.append(await timed(heavy_client, REGISTER, new_credentials(), 201))
        taken.append(
            await timed(
                heavy_client, REGISTER, new_credentials(email=owner.credentials["email"]), 201
            )
        )

    median_fresh, median_taken = statistics.median(fresh), statistics.median(taken)
    assert median_fresh > 0.02  # хэширование заметно, значит сравнение осмысленно
    assert abs(median_fresh - median_taken) < 0.3 * median_fresh, (median_fresh, median_taken)


async def test_login_time_does_not_reveal_whether_the_account_exists(
    heavy_client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(heavy_client, jobs)
    email = user.credentials["email"]
    wrong_password: list[float] = []
    unknown_login: list[float] = []

    for n in range(8):
        wrong_password.append(
            await timed(
                heavy_client, LOGIN, {"login": email, "password": f"wrong-password-{n}"}, 401
            )
        )
        unknown_login.append(
            await timed(
                heavy_client,
                LOGIN,
                {"login": f"nobody-{n}@example.com", "password": f"wrong-password-{n}"},
                401,
            )
        )

    median_wrong, median_unknown = (
        statistics.median(wrong_password),
        statistics.median(unknown_login),
    )
    assert median_wrong > 0.02
    assert abs(median_wrong - median_unknown) < 0.3 * median_wrong, (median_wrong, median_unknown)


async def test_parallel_logins_do_not_block_the_event_loop(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    """Пока считаются хэши паролей, цикл событий продолжает отвечать (acceptance S1)."""
    settings = test_settings.model_copy(
        update={
            "argon2_time_cost": 6,
            "argon2_memory_cost_kib": 65_536,
            "argon2_parallelism": 1,
            "password_hash_concurrency": 4,
        }
    )
    application = create_app(settings, job_queue=jobs)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            user = await verified_user(http, jobs)
            body = {"login": user.credentials["email"], "password": "wrong password here"}
            # Прогрев: первый запрос по пути входа компилирует SQL-выражения и строит планы записи
            # (десятки миллисекунд синхронной работы), к хэшированию это отношения не имеет.
            assert (await http.post(LOGIN, json=body)).status_code == 401
            worst_lag = 0.0
            stop = asyncio.Event()

            async def ticker() -> None:
                nonlocal worst_lag
                loop = asyncio.get_running_loop()
                previous = loop.time()
                while not stop.is_set():
                    await asyncio.sleep(0.002)
                    now = loop.time()
                    worst_lag = max(worst_lag, now - previous - 0.002)
                    previous = now

            async def login_after(delay: float) -> httpx.Response:
                # Запросы идут со сдвигом: иначе работа самого FastAPI по восьми одновременным
                # запросам (разбор, проверка, зависимости) слилась бы в одну долгую итерацию цикла и
                # дала бы задержку, не связанную с хэшированием.
                await asyncio.sleep(delay)
                return await http.post(LOGIN, json=body)

            tick = asyncio.create_task(ticker())
            started = time.perf_counter()
            responses = await asyncio.gather(*(login_after(n * 0.02) for n in range(8)))
            elapsed = time.perf_counter() - started
            stop.set()
            await tick

    assert {r.status_code for r in responses} == {401}
    assert elapsed > 0.2  # проверка паролей заметно длится
    # Блокирующий хэш (около 0,2 с каждый) дал бы простой не меньше 0,2 с.
    assert worst_lag < 0.08, f"цикл событий простоял {worst_lag:.3f} с"
    assert worst_lag < elapsed / 3

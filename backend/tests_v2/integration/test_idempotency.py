"""`Idempotency-Key` (S2-07): повтор, подмена тела, параллельные дубли, ключи разных пользователей, сбои.

Реальных создающих ручек в S2 ещё нет (первая появится в S5), поэтому к приложению подключается
тестовый роутер с тем же классом маршрута, каким будут пользоваться ручки соцсети.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError
from messunjerr.core.idempotency import idempotent_route_class
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.api.deps import PrincipalDep, principal_user_id
from messunjerr.main import create_app
from messunjerr.settings import Settings

from .helpers import SignedInUser, execute, fetch_all, fetch_one, verified_user

THINGS = "/_test/things"
REPLAYED = "idempotency-replayed"


@dataclass
class Probe:
    """Что делала тестовая ручка: сколько раз выполнялась и как себя вести дальше."""

    calls: int = 0
    fail_next: str | None = None
    """`"raise"`: бросить ошибку; `"return"`: вернуть `409` готовым ответом."""
    delay: float = 0.0
    gate: asyncio.Event | None = None
    """Если задан, ручка ждёт его: так тест держит первый запрос «в полёте»."""
    entered: asyncio.Event = field(default_factory=asyncio.Event)


def build_router(probe: Probe) -> APIRouter:
    router = APIRouter(prefix="/_test", route_class=idempotent_route_class(principal_user_id))

    @router.post("/things", status_code=201, response_model=None)
    async def create_thing(
        body: dict[str, Any], response: Response, principal: PrincipalDep
    ) -> dict[str, Any] | Response:
        probe.calls += 1
        number = probe.calls
        probe.entered.set()
        if probe.gate is not None:
            await probe.gate.wait()
        if probe.delay:
            await asyncio.sleep(probe.delay)
        if probe.fail_next == "raise":
            probe.fail_next = None
            raise DomainError(ErrorCode.SERVICE_UNAVAILABLE)
        if probe.fail_next == "return":
            probe.fail_next = None
            return JSONResponse({"refused": True}, status_code=409)
        response.headers["Location"] = f"/_test/things/{number}"
        return {"number": number, "owner": str(principal.user_id), "echo": body}

    @router.get("/things")
    async def count_things(principal: PrincipalDep) -> dict[str, int]:
        probe.calls += 1
        return {"calls": probe.calls}

    return router


@asynccontextmanager
async def probe_client(
    settings: Settings, jobs: InMemoryJobQueue
) -> AsyncGenerator[tuple[httpx.AsyncClient, Probe]]:
    application = create_app(settings, job_queue=jobs)
    probe = Probe()
    application.include_router(build_router(probe))
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, probe


@pytest_asyncio.fixture
async def probe_app(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> AsyncIterator[tuple[httpx.AsyncClient, Probe]]:
    async with probe_client(test_settings, jobs) as pair:
        yield pair


def new_key() -> str:
    return str(uuid.uuid4())


def keyed(user: SignedInUser, key: str) -> dict[str, str]:
    return {**user.headers, "Idempotency-Key": key}


# ----------------------------------------------------------------------------- повтор
async def test_a_repeat_with_the_same_key_gets_the_first_answer_without_running_again(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()

    first = await http.post(THINGS, json={"title": "one"}, headers=keyed(user, key))
    second = await http.post(THINGS, json={"title": "one"}, headers=keyed(user, key))

    assert (first.status_code, second.status_code) == (201, 201)
    expected = {"number": 1, "owner": user.user_id, "echo": {"title": "one"}}
    assert first.json() == expected
    assert second.json() == expected
    assert second.headers["location"] == first.headers["location"] == "/_test/things/1"
    assert REPLAYED not in first.headers
    assert second.headers[REPLAYED] == "true"
    assert probe.calls == 1


async def test_without_the_header_every_request_runs(
    probe_app: tuple[httpx.AsyncClient, Probe],
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    http, _ = probe_app
    user = await verified_user(http, jobs)

    first = await http.post(THINGS, json={}, headers=user.headers)
    second = await http.post(THINGS, json={}, headers=user.headers)

    assert (first.json()["number"], second.json()["number"]) == (1, 2)
    assert REPLAYED not in second.headers
    assert await fetch_all(admin_engine, "SELECT 1 FROM platform.idempotency_keys") == []


async def test_different_keys_are_different_requests(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)

    first = await http.post(THINGS, json={"a": 1}, headers=keyed(user, new_key()))
    second = await http.post(THINGS, json={"a": 1}, headers=keyed(user, new_key()))

    assert (first.json()["number"], second.json()["number"]) == (1, 2)
    assert probe.calls == 2


async def test_json_formatting_does_not_make_a_repeat_a_different_request(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    headers = {**keyed(user, key), "Content-Type": "application/json"}

    first = await http.post(THINGS, content=b'{"b": 2,   "a": 1}', headers=headers)
    second = await http.post(THINGS, content=b'{"a":1,"b":2}', headers=headers)

    assert first.status_code == 201
    assert second.headers[REPLAYED] == "true"
    assert second.json() == first.json()
    assert probe.calls == 1


async def test_get_requests_ignore_the_header(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, _ = probe_app
    user = await verified_user(http, jobs)
    key = new_key()

    first = await http.get(THINGS, headers=keyed(user, key))
    second = await http.get(THINGS, headers=keyed(user, key))

    assert (first.json()["calls"], second.json()["calls"]) == (1, 2)
    assert REPLAYED not in second.headers


# ----------------------------------------------------------------------------- подмена запроса
async def test_the_same_key_with_another_body_is_refused_and_keeps_the_original(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()

    first = await http.post(THINGS, json={"title": "one"}, headers=keyed(user, key))
    changed = await http.post(THINGS, json={"title": "two"}, headers=keyed(user, key))
    again = await http.post(THINGS, json={"title": "one"}, headers=keyed(user, key))

    assert first.status_code == 201
    assert changed.status_code == 422
    assert changed.json()["code"] == "idempotency_key_reuse"
    assert changed.headers["content-type"] == "application/problem+json"
    assert again.headers[REPLAYED] == "true"  # подмена не затёрла сохранённый ответ
    assert again.json() == first.json()
    assert probe.calls == 1


async def test_the_same_key_with_another_query_is_refused(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()

    await http.post(THINGS, json={}, headers=keyed(user, key))
    other = await http.post(f"{THINGS}?draft=1", json={}, headers=keyed(user, key))

    assert other.status_code == 422
    assert other.json()["code"] == "idempotency_key_reuse"
    assert probe.calls == 1


async def test_keys_belong_to_the_user_who_made_them(
    probe_app: tuple[httpx.AsyncClient, Probe],
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    http, probe = probe_app
    alice = await verified_user(http, jobs)
    bob = await verified_user(http, jobs)
    key = new_key()

    from_alice = await http.post(THINGS, json={"x": 1}, headers=keyed(alice, key))
    from_bob = await http.post(THINGS, json={"x": 1}, headers=keyed(bob, key))
    alice_again = await http.post(THINGS, json={"x": 1}, headers=keyed(alice, key))

    assert from_alice.json()["owner"] == alice.user_id
    assert from_bob.json()["owner"] == bob.user_id  # чужой ключ не воспроизвёл чужой ответ
    assert REPLAYED not in from_bob.headers
    assert alice_again.headers[REPLAYED] == "true"
    assert alice_again.json() == from_alice.json()
    assert probe.calls == 2
    rows = await fetch_all(admin_engine, "SELECT user_id FROM platform.idempotency_keys")
    assert len(rows) == 2


# ----------------------------------------------------------------------------- параллельные запросы
async def test_a_parallel_duplicate_gets_409_while_the_first_one_is_running(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue, redis_client: Redis
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    lock_key = f"idem:lock:{user.user_id}:{key}"
    probe.gate = asyncio.Event()

    first = asyncio.create_task(http.post(THINGS, json={"a": 1}, headers=keyed(user, key)))
    await asyncio.wait_for(probe.entered.wait(), timeout=5)

    duplicate = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
    swapped = await http.post(THINGS, json={"a": 2}, headers=keyed(user, key))
    ttl = await redis_client.ttl(lock_key)
    probe.gate.set()
    finished = await first
    after = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "request_in_progress"
    assert duplicate.headers["retry-after"] == "1"
    assert swapped.status_code == 422  # пока первый в работе, подмена тела тоже видна
    assert swapped.json()["code"] == "idempotency_key_reuse"
    assert 0 < ttl <= 60  # замок с запасом на зависший запрос
    assert finished.status_code == 201
    assert after.headers[REPLAYED] == "true"
    assert after.json() == finished.json()
    assert await redis_client.exists(lock_key) == 0  # замок снят после завершения
    assert probe.calls == 1


async def test_twenty_parallel_duplicates_run_the_handler_once(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    probe.delay = 0.3

    results = await asyncio.gather(
        *(http.post(THINGS, json={"a": 1}, headers=keyed(user, key)) for _ in range(20))
    )

    statuses = Counter(response.status_code for response in results)
    assert probe.calls == 1
    assert set(statuses) <= {201, 409}
    assert statuses[201] >= 1
    assert {response.json()["number"] for response in results if response.status_code == 201} == {1}


# ----------------------------------------------------------------------------- сбои и ошибки
@pytest.mark.parametrize("failure", ["raise", "return"])
async def test_a_failed_attempt_is_not_remembered_and_the_key_stays_usable(
    probe_app: tuple[httpx.AsyncClient, Probe],
    jobs: InMemoryJobQueue,
    redis_client: Redis,
    admin_engine: AsyncEngine,
    failure: str,
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    probe.fail_next = failure

    failed = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
    assert failed.status_code in (409, 503)
    assert await redis_client.exists(f"idem:lock:{user.user_id}:{key}") == 0
    assert await fetch_all(admin_engine, "SELECT 1 FROM platform.idempotency_keys") == []

    retried = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
    again = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))

    assert retried.status_code == 201
    assert REPLAYED not in retried.headers
    assert again.headers[REPLAYED] == "true"
    assert probe.calls == 2


async def test_an_invalid_key_is_a_validation_error(
    probe_app: tuple[httpx.AsyncClient, Probe], jobs: InMemoryJobQueue
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)

    for value in ("not-a-uuid", "12345", ""):
        response = await http.post(THINGS, json={}, headers=keyed(user, value))
        body = response.json()
        assert response.status_code == 422, value
        assert body["code"] == "validation_error"
        assert body["errors"][0]["pointer"] == "/header/Idempotency-Key"
    assert probe.calls == 0


async def test_authentication_comes_first(
    probe_app: tuple[httpx.AsyncClient, Probe], admin_engine: AsyncEngine
) -> None:
    http, probe = probe_app

    anonymous = await http.post(THINGS, json={}, headers={"Idempotency-Key": new_key()})
    garbage_key = await http.post(THINGS, json={}, headers={"Idempotency-Key": "nope"})
    forged = await http.post(
        THINGS,
        json={},
        headers={"Authorization": "Bearer not.a.token", "Idempotency-Key": new_key()},
    )

    assert [r.status_code for r in (anonymous, garbage_key, forged)] == [401, 401, 401]
    assert probe.calls == 0
    assert await fetch_all(admin_engine, "SELECT 1 FROM platform.idempotency_keys") == []


async def test_what_is_stored_and_for_how_long(
    probe_app: tuple[httpx.AsyncClient, Probe],
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    http, _ = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    sent = await http.post(THINGS, json={"secret": "do not keep"}, headers=keyed(user, key))

    row = await fetch_one(
        admin_engine,
        "SELECT user_id, key, request_hash, response_status, response_body,"
        " expires_at - created_at AS lifetime FROM platform.idempotency_keys",
    )

    assert str(row["user_id"]) == user.user_id
    assert row["key"] == key
    assert row["response_status"] == 201
    assert row["response_body"] == {
        "body": sent.json(),
        "headers": {"location": "/_test/things/1"},
    }
    assert len(bytes(row["request_hash"])) == 32  # хэш запроса, а не сам запрос
    assert timedelta(hours=23, minutes=59) < row["lifetime"] < timedelta(hours=24, minutes=1)


async def test_an_expired_record_is_forgotten_and_the_key_can_be_used_again(
    probe_app: tuple[httpx.AsyncClient, Probe],
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
) -> None:
    http, probe = probe_app
    user = await verified_user(http, jobs)
    key = new_key()
    first = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
    await execute(
        admin_engine,
        "UPDATE platform.idempotency_keys SET expires_at = now() - interval '1 minute'",
    )

    second = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
    third = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))

    assert first.json()["number"] == 1
    assert second.json()["number"] == 2  # запись просрочена: запрос выполнен заново
    assert REPLAYED not in second.headers
    assert third.headers[REPLAYED] == "true"  # и новый ответ тоже сохранён
    assert third.json() == second.json()
    assert probe.calls == 2


async def test_without_redis_the_request_runs_and_a_later_repeat_is_still_replayed(
    test_settings: Settings, jobs: InMemoryJobQueue, client: httpx.AsyncClient
) -> None:
    user = await verified_user(client, jobs)  # аккаунт создаём в здоровом приложении
    broken = test_settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    key = new_key()

    async with probe_client(broken, jobs) as (http, probe):
        first = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))
        second = await http.post(THINGS, json={"a": 1}, headers=keyed(user, key))

    # Замок недоступен, но сохранённый в PostgreSQL ответ воспроизводится.
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.headers[REPLAYED] == "true"
    assert second.json() == first.json()
    assert probe.calls == 1

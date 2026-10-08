"""`/metrics` и счётчики (S6-06): запросы по шаблонам маршрутов, пул БД, очереди, отказы лимитов и входа."""

import asyncio
import socket
import urllib.request
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from messunjerr.core.deps import AppResources
from messunjerr.core.jobs import QUEUE_DEFAULT, InMemoryJobQueue
from messunjerr.jobs import worker as worker_module
from messunjerr.jobs.health import queue_key
from messunjerr.jobs.worker import build_worker
from messunjerr.settings import Settings

from .helpers import LOGIN, limited_client, verified_user


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def scrape(client: httpx.AsyncClient) -> str:
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    return response.text


def line_value(body: str, series: str) -> float:
    for line in body.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"в метриках нет {series!r}")


async def test_the_endpoint_serves_prometheus_text_with_the_main_series(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/api/v1/meta")

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    for name in (
        "http_requests_total",
        "http_request_duration_seconds_bucket",
        "db_pool_in_use",
        "db_pool_size",
        "arq_queue_depth",
        "process_resident_memory_bytes",
    ):
        assert name in body, name
    assert "# HELP http_requests_total" in body


async def test_the_endpoint_is_not_in_the_public_documentation(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    assert "/metrics" not in schema["paths"]


async def test_requests_are_counted_by_route_template_method_and_status(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    meta = {"route": "/api/v1/meta", "method": "GET", "status": "200"}
    asset = {"route": "/api/v1/media/{asset_id}", "method": "GET", "status": "404"}
    meta_before = sample("http_requests_total", **meta)
    asset_before = sample("http_requests_total", **asset)
    timed_before = sample("http_request_duration_seconds_count", route="/api/v1/meta", method="GET")

    await client.get("/api/v1/meta")
    await client.get("/api/v1/meta")
    await client.get("/api/v1/media/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80", headers=user.headers)
    await client.get("/api/v1/media/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d81", headers=user.headers)

    assert sample("http_requests_total", **meta) == meta_before + 2
    # Идентификатор в метку не попадает: оба запроса учтены под шаблоном маршрута.
    assert sample("http_requests_total", **asset) == asset_before + 2
    assert (
        sample("http_request_duration_seconds_count", route="/api/v1/meta", method="GET")
        == timed_before + 2
    )
    assert "0192b7a0" not in await scrape(client)


async def test_unknown_paths_and_odd_methods_do_not_multiply_the_series(
    client: httpx.AsyncClient,
) -> None:
    unmatched = {"route": "unmatched", "method": "GET", "status": "404"}
    odd = {"route": "/api/v1/meta", "method": "OTHER", "status": "405"}
    before = (sample("http_requests_total", **unmatched), sample("http_requests_total", **odd))

    for n in range(5):
        await client.get(f"/no/such/place/{n}")  # чужие пути: одна метка на все
    await client.request("PROPFIND", "/api/v1/meta")

    assert sample("http_requests_total", **unmatched) == before[0] + 5
    assert sample("http_requests_total", **odd) == before[1] + 1
    assert "no/such/place" not in await scrape(client)


async def test_scraping_does_not_count_itself(client: httpx.AsyncClient) -> None:
    await scrape(client)
    await scrape(client)

    assert 'route="/metrics"' not in await scrape(client)


async def test_a_checked_out_connection_shows_up_in_the_pool_gauge(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    resources: AppResources = app.state.resources
    idle = line_value(await scrape(client), "db_pool_in_use")

    async with resources.sessionmaker() as session:
        await session.execute(text("SELECT 1"))  # соединение занято до конца блока
        busy = line_value(await scrape(client), "db_pool_in_use")

    assert busy == idle + 1
    assert line_value(await scrape(client), "db_pool_size") == resources.settings.db_pool_size


async def test_the_queue_gauge_counts_waiting_jobs_per_queue(
    client: httpx.AsyncClient, redis_client: Redis
) -> None:
    await redis_client.zadd(queue_key("media"), {"job-a": 1, "job-b": 2})  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]
    await redis_client.zadd(queue_key(QUEUE_DEFAULT), {"job-c": 1})  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]

    body = await scrape(client)

    assert line_value(body, 'arq_queue_depth{queue="media"}') == 2
    assert line_value(body, 'arq_queue_depth{queue="default"}') == 1
    assert line_value(body, 'arq_queue_depth{queue="email"}') == 0


async def test_metrics_survive_a_redis_outage_without_the_queue_series(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_args: Any, **_kwargs: Any) -> int:
        raise RedisError("redis is down")

    monkeypatch.setattr(app.state.resources.redis, "zcard", down)

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "db_pool_in_use" in response.text
    assert "arq_queue_depth{" not in response.text


async def test_failed_logins_are_counted_by_reason(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    bad = sample("auth_failures_total", reason="bad_password")
    unknown = sample("auth_failures_total", reason="unknown_login")

    await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": "wrong password!"}
    )
    await client.post(LOGIN, json={"login": "nobody@example.com", "password": "wrong password!"})
    ok = await client.post(
        LOGIN, json={"login": user.credentials["email"], "password": user.credentials["password"]}
    )

    assert ok.status_code == 200
    assert sample("auth_failures_total", reason="bad_password") == bad + 1
    assert sample("auth_failures_total", reason="unknown_login") == unknown + 1


async def test_refused_requests_are_counted_by_bucket(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    before = sample("rate_limited_total", bucket="auth_login_ip")

    async with limited_client(test_settings, jobs, auth_login_ip=2) as (_, http):
        for n in range(4):
            await http.post(
                LOGIN, json={"login": f"nobody-{n}@example.com", "password": "wrong password!"}
            )

    assert sample("rate_limited_total", bucket="auth_login_ip") >= before + 2


# ----------------------------------------------------------------------------- воркеры
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_worker_publishes_its_metrics_on_its_own_port_while_it_runs(
    test_settings: Settings,
) -> None:
    port = free_port()
    context: dict[str, Any] = {
        "queue": QUEUE_DEFAULT,
        "settings": test_settings.model_copy(update={"worker_metrics_port": port}),
    }

    await worker_module._on_startup(context)  # pyright: ignore[reportPrivateUsage]
    try:
        page = await asyncio.to_thread(
            lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
        )
        assert b"arq_jobs_total" in page or b"http_requests_total" in page
    finally:
        await worker_module._on_shutdown(context)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(OSError):  # noqa: PT011
        await asyncio.to_thread(
            lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
        )


async def test_a_worker_without_a_metrics_port_publishes_nothing(test_settings: Settings) -> None:
    context: dict[str, Any] = {"queue": QUEUE_DEFAULT, "settings": test_settings}

    await worker_module._on_startup(context)  # pyright: ignore[reportPrivateUsage]
    try:
        assert context["exporter"] is None
    finally:
        await worker_module._on_shutdown(context)  # pyright: ignore[reportPrivateUsage]


async def test_a_busy_metrics_port_does_not_keep_the_worker_from_starting(
    test_settings: Settings,
) -> None:
    """Порт метрик вспомогательный: из-за него нельзя оставить без воркера письма и файлы."""
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = int(occupied.getsockname()[1])
        context: dict[str, Any] = {
            "queue": QUEUE_DEFAULT,
            "settings": test_settings.model_copy(update={"worker_metrics_port": port}),
        }

        await worker_module._on_startup(context)  # pyright: ignore[reportPrivateUsage]
        try:
            assert context["exporter"] is None  # метрик нет, воркер работает
            assert "jobs" in context
        finally:
            await worker_module._on_shutdown(context)  # pyright: ignore[reportPrivateUsage]


def test_the_media_queue_takes_two_jobs_at_once_and_the_others_keep_the_default(
    test_settings: Settings,
) -> None:
    media = build_worker("media", test_settings, burst=True, handle_signals=False)
    email = build_worker("email", test_settings, burst=True, handle_signals=False)
    default = build_worker("default", test_settings, burst=True, handle_signals=False)

    assert media.max_jobs == 2
    assert email.max_jobs == default.max_jobs == worker_module.DEFAULT_MAX_JOBS

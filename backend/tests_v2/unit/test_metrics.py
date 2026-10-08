"""Метрики Prometheus (S6-06): метки, снимок, учёт задач воркера, порт метрик."""

import asyncio
import urllib.request
from dataclasses import dataclass
from typing import Any, cast

import pytest
from arq.worker import Retry
from prometheus_client import REGISTRY
from starlette.types import Scope

from messunjerr.core.metrics import (
    Exporter,
    Snapshot,
    http_method_label,
    render_metrics,
    start_exporter,
)
from messunjerr.core.middleware import route_template
from messunjerr.core.ratelimit import RateLimitResult, rate_limited
from messunjerr.jobs.instrument import instrumented


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.parametrize(
    ("method", "label"),
    [
        ("GET", "GET"),
        ("POST", "POST"),
        ("PATCH", "PATCH"),
        ("OPTIONS", "OPTIONS"),
        ("get", "OTHER"),  # методы регистрозависимы; чужие значения в метки не идут
        ("PROPFIND", "OTHER"),
        ("A" * 500, "OTHER"),
        (None, "OTHER"),
    ],
)
def test_http_methods_are_limited_to_a_known_set(method: str | None, label: str) -> None:
    assert http_method_label(method) == label


def test_the_exposition_has_the_process_metrics_and_the_snapshot() -> None:
    body = render_metrics(
        Snapshot(pool_in_use=3, pool_size=10, queue_depth={"media": 4, "email": 0})
    ).decode()

    assert "# TYPE http_requests_total counter" in body
    assert "# TYPE http_request_duration_seconds histogram" in body
    assert "process_cpu_seconds_total" in body  # стандартные метрики процесса
    assert "db_pool_in_use 3.0" in body
    assert "db_pool_size 10.0" in body
    assert 'arq_queue_depth{queue="media"} 4.0' in body
    assert 'arq_queue_depth{queue="email"} 0.0' in body


def test_without_a_snapshot_only_the_process_metrics_are_given() -> None:
    body = render_metrics().decode()

    assert "http_requests_total" in body
    assert "db_pool_in_use" not in body
    assert "arq_queue_depth" not in body


def test_a_refused_request_is_counted_by_bucket() -> None:
    before = sample("rate_limited_total", bucket="pytest_bucket")
    result = RateLimitResult(
        "pytest_bucket", allowed=False, limit=5, remaining=0, retry_after=3, reset=9
    )

    error = rate_limited(result)

    assert error.headers["Retry-After"] == "3"
    assert sample("rate_limited_total", bucket="pytest_bucket") == before + 1


# ----------------------------------------------------------------------------- задачи воркера
async def _noop_returning() -> int:
    return 42


async def test_a_task_is_counted_as_completed_and_timed() -> None:
    before = sample("arq_jobs_total", job="pytest_ok", outcome="completed")
    runs = sample("arq_job_duration_seconds_count", job="pytest_ok")

    assert await instrumented("pytest_ok", _noop_returning)() == 42

    assert sample("arq_jobs_total", job="pytest_ok", outcome="completed") == before + 1
    assert sample("arq_job_duration_seconds_count", job="pytest_ok") == runs + 1
    assert sample("arq_jobs_failed_total", job="pytest_ok") == 0


async def _explodes() -> None:
    raise RuntimeError("boom")


async def _asks_for_retry() -> None:
    raise Retry(defer=30)


async def _hangs() -> None:
    await asyncio.sleep(60)


async def test_a_failure_is_counted_and_still_raised() -> None:
    failed = sample("arq_jobs_failed_total", job="pytest_fail")

    with pytest.raises(RuntimeError, match="boom"):
        await instrumented("pytest_fail", _explodes)()

    assert sample("arq_jobs_failed_total", job="pytest_fail") == failed + 1
    assert sample("arq_jobs_total", job="pytest_fail", outcome="failed") >= 1
    assert sample("arq_job_duration_seconds_count", job="pytest_fail") >= 1


async def test_a_retry_request_is_not_a_failure() -> None:
    with pytest.raises(Retry):
        await instrumented("pytest_retry", _asks_for_retry)()

    assert sample("arq_jobs_total", job="pytest_retry", outcome="retried") == 1
    assert sample("arq_jobs_failed_total", job="pytest_retry") == 0


async def test_a_cancelled_task_is_not_blamed_on_the_task() -> None:
    task = asyncio.create_task(instrumented("pytest_cancel", _hangs)())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sample("arq_jobs_failed_total", job="pytest_cancel") == 0
    assert sample("arq_jobs_total", job="pytest_cancel", outcome="failed") == 0


async def test_the_wrapper_keeps_the_name_and_arguments_of_the_task() -> None:
    async def send(ctx: dict[str, int], *, to: str) -> str:
        return f"{ctx['n']}:{to}"

    wrapped = instrumented("pytest_args", send)

    assert wrapped.__name__ == "send"
    assert await wrapped({"n": 1}, to="a") == "1:a"


# ----------------------------------------------------------------------------- порт метрик
def test_the_exporter_serves_the_registry_on_its_own_port_and_stops() -> None:
    exporter = Exporter(0)  # порт назначает система
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{exporter.port}/metrics", timeout=5) as page:
            body = page.read().decode()
            assert page.status == 200
        assert "arq_jobs_total" in body or "http_requests_total" in body
    finally:
        exporter.stop()
    with pytest.raises(OSError):  # noqa: PT011 (причина зависит от ОС: отказ или обрыв)
        urllib.request.urlopen(f"http://127.0.0.1:{exporter.port}/metrics", timeout=2)


def test_port_zero_means_no_exporter() -> None:
    assert start_exporter(0) is None


# ----------------------------------------------------------------------------- шаблон маршрута
@dataclass
class FakeRoute:
    """Маршрут FastAPI 0.142: путь относительный, `path_format` собирается из параметров запроса."""

    path: Any
    path_format: Any = None


def scope_of(route: object | None, path: str, **params: str) -> Scope:
    scope: dict[str, Any] = {"type": "http", "path": path, "path_params": params}
    if route is not None:
        scope["route"] = route
    return cast(Scope, scope)


def test_the_full_template_is_rebuilt_from_the_request_path() -> None:
    route = FakeRoute("/media/{asset_id}", "/media/{asset_id}")

    assert (
        route_template(scope_of(route, "/api/v1/media/0190-abc", asset_id="0190-abc"))
        == "/api/v1/media/{asset_id}"
    )
    # Параметров несколько, в том числе похожих на префикс: подставляются именно значения.
    nested = FakeRoute("/users/{ref}/posts/{post_id}", "/users/{ref}/posts/{post_id}")
    assert (
        route_template(scope_of(nested, "/api/v1/users/media/posts/7", ref="media", post_id="7"))
        == "/api/v1/users/{ref}/posts/{post_id}"
    )


def test_a_request_without_a_route_has_no_template() -> None:
    assert route_template(scope_of(None, "/nowhere")) is None
    assert route_template(scope_of(FakeRoute(path=None), "/nowhere")) is None


def test_a_route_that_cannot_be_rebuilt_gives_what_it_has() -> None:
    # Нет `path_format` (не маршрут пути, а, например, монтирование): отдаётся относительный шаблон.
    assert route_template(scope_of(FakeRoute("/static"), "/static/a.css")) == "/static"
    # Параметр маршрута не пришёл с запросом: формат не собирается.
    broken = FakeRoute("/media/{asset_id}", "/media/{asset_id}")
    assert route_template(scope_of(broken, "/api/v1/media/1")) == "/media/{asset_id}"
    # Путь запроса не оканчивается собранной частью (перенаправление, слэш на конце).
    slashed = FakeRoute("/media/{asset_id}", "/media/{asset_id}")
    assert (
        route_template(scope_of(slashed, "/api/v1/media/1/", asset_id="1")) == "/media/{asset_id}"
    )

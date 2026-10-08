"""Метрики Prometheus (4.15, S6-06): счётчики и гистограммы процесса и снимок «на момент опроса».

Два вида метрик:

- **накопительные** живут в реестре процесса (`prometheus_client.REGISTRY` вместе со стандартными
  метриками процесса, сборщика мусора и платформы) и обновляются там, где событие случилось: запрос
  HTTP, задача воркера, отказ лимита, обработка файла. API отдаёт их на `/metrics`; у каждого
  воркера своё число, и его отдаёт собственный порт (`start_exporter`, настройка
  `WORKER_METRICS_PORT`): Prometheus суммирует процессы сам;
- **снимок** (занятые соединения пула БД, глубина очередей arq) читается при каждом опросе
  (`Snapshot`), потому что это состояние, а не событие.

Снаружи `/metrics` недоступен: Caddy отвечает `404` (4.15). Значения меток ограничены: маршрут это
шаблон пути, а не сам путь, метод из известного списка, причина и бакет из закрытых перечней.
"""

from dataclasses import dataclass, field
from typing import Final

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4
from prometheus_client.registry import Collector

__all__ = [
    "ARQ_JOBS",
    "ARQ_JOBS_FAILED",
    "ARQ_JOB_SECONDS",
    "AUTH_FAILURES",
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "MEDIA_PROCESSING_SECONDS",
    "MEDIA_REJECTED",
    "METRICS_CONTENT_TYPE",
    "RATE_LIMITED",
    "Exporter",
    "Snapshot",
    "http_method_label",
    "render_metrics",
    "start_exporter",
]

_HTTP_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_JOB_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 600.0)
_KNOWN_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
METRICS_CONTENT_TYPE: Final = CONTENT_TYPE_PLAIN_0_0_4
"""Формат 0.0.4: его понимают все версии Prometheus (в новом 1.0.0 имена могут быть и не ASCII, а у нас они
всегда ASCII)."""
UNMATCHED_ROUTE: Final = "unmatched"
"""Метка для запросов, которым не нашлось маршрута (404 на чужие пути): пути в метки не идут."""

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Запросы HTTP по шаблону маршрута, методу и статусу.",
    ["route", "method", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Длительность обработки запроса HTTP.",
    ["route", "method"],
    buckets=_HTTP_BUCKETS,
)
RATE_LIMITED = Counter(
    "rate_limited_total", "Запросы, отклонённые лимитом (429), по бакету.", ["bucket"]
)
AUTH_FAILURES = Counter("auth_failures_total", "Неудачные попытки входа по причине.", ["reason"])
MEDIA_PROCESSING_SECONDS = Histogram(
    "media_processing_seconds",
    "Обработка загруженного файла воркером media: вид файла и итог.",
    ["kind", "outcome"],
    buckets=_JOB_BUCKETS,
)
MEDIA_REJECTED = Counter(
    "media_rejected_total", "Файлы, отклонённые обработкой, по причине.", ["reason"]
)
ARQ_JOBS = Counter(
    "arq_jobs_total",
    "Запуски задач воркера: completed, failed или retried.",
    ["job", "outcome"],
)
ARQ_JOBS_FAILED = Counter(
    "arq_jobs_failed_total", "Задачи воркера, закончившиеся ошибкой.", ["job"]
)
ARQ_JOB_SECONDS = Histogram(
    "arq_job_duration_seconds",
    "Длительность запуска задачи воркера.",
    ["job"],
    buckets=_JOB_BUCKETS,
)


def http_method_label(method: str | None) -> str:
    """Метод запроса для метки: неизвестные значения сводятся к `OTHER` (иначе меток бесконечно много)."""
    return method if method in _KNOWN_METHODS else "OTHER"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Состояние на момент опроса: то, что нельзя накопить событиями."""

    pool_in_use: int
    pool_size: int
    queue_depth: dict[str, int] = field(default_factory=dict[str, int])
    """Глубина очереди arq по имени (задачи, которые ещё никто не взял)."""


class _SnapshotCollector(Collector):
    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    def collect(self) -> list[GaugeMetricFamily]:
        pool = GaugeMetricFamily("db_pool_in_use", "Занятые соединения пула PostgreSQL.")
        pool.add_metric([], self._snapshot.pool_in_use)
        size = GaugeMetricFamily("db_pool_size", "Размер пула PostgreSQL (без запаса).")
        size.add_metric([], self._snapshot.pool_size)
        depth = GaugeMetricFamily(
            "arq_queue_depth", "Задач в очереди arq, которые ещё никто не взял.", labels=["queue"]
        )
        for queue, value in sorted(self._snapshot.queue_depth.items()):
            depth.add_metric([queue], value)
        return [pool, size, depth]


def render_metrics(snapshot: Snapshot | None = None) -> bytes:
    """Текст для Prometheus: метрики процесса и, если дан, снимок состояния."""
    body = generate_latest(REGISTRY)
    if snapshot is None:
        return body
    scratch = CollectorRegistry()
    scratch.register(_SnapshotCollector(snapshot))
    return body + generate_latest(scratch)


class Exporter:
    """Маленький HTTP-сервер метрик для процесса без собственного веб-сервера (воркеры)."""

    def __init__(self, port: int) -> None:
        self._server, self._thread = start_http_server(port, registry=REGISTRY)

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def start_exporter(port: int) -> Exporter | None:
    """Поднимает порт метрик; `0` значит «не нужен»."""
    return Exporter(port) if port > 0 else None

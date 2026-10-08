"""Служебные ручки (5.13): здоровье, метрики и параметры для клиента."""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.pool import QueuePool

from messunjerr import __version__
from messunjerr.core.clock import utcnow
from messunjerr.core.deps import AppResources, ResourcesDep
from messunjerr.core.health import run_readiness
from messunjerr.core.jobs import QUEUES
from messunjerr.core.limits import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, limits_for_meta
from messunjerr.core.logs import get_logger
from messunjerr.core.metrics import METRICS_CONTENT_TYPE, Snapshot, render_metrics
from messunjerr.core.schemas import UtcDateTime
from messunjerr.jobs.health import queue_key

# Снаружи эти адреса закрывает Caddy (404): они нужны только оркестратору и мониторингу.
health_router = APIRouter(tags=["service"])
api_router = APIRouter(prefix="/api/v1", tags=["service"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ServingResponse(BaseModel):
    status: Literal["serving", "draining"]


class ReadyResponse(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, str]
    degraded: dict[str, str]


class MetaAuth(BaseModel):
    methods: list[str]
    oauth_providers: list[str]
    password_min_length: int
    password_max_length: int


class MetaFeatures(BaseModel):
    email_notifications: bool
    data_export: bool


class MetaLegalDocument(BaseModel):
    slug: str
    version: str


class MetaLegal(BaseModel):
    min_age: int
    documents: list[MetaLegalDocument]


class MetaResponse(BaseModel):
    version: str
    build: str
    server_time: UtcDateTime
    limits: dict[str, int]
    reactions: list[str]
    auth: MetaAuth
    legal: MetaLegal
    features: MetaFeatures


@health_router.get("/health/live", response_model=LiveResponse, summary="Процесс жив")
async def live() -> LiveResponse:
    return LiveResponse()


@health_router.get(
    "/health/serving",
    response_model=ServingResponse,
    responses={
        503: {"model": ServingResponse, "description": "Процесс закрывается (слив трафика)"}
    },
    summary="Принимает трафик (для балансировщика)",
)
async def serving(resources: ResourcesDep, response: Response) -> ServingResponse:
    # Только слив при остановке: по этой ручке Caddy снимает реплику с балансировки раньше, чем
    # процесс перестанет принимать соединения (S4-03). PostgreSQL и Redis здесь намеренно не
    # проверяются: общий сбой зависимости вывел бы из балансировки обе реплики сразу, и весь
    # `/api/*` отвечал бы 503 после ожидания Caddy, а так каждая реплика отвечает ошибкой сама.
    # `/health/live` при сливе остаётся 200 (иначе Docker счёл бы реплику упавшей).
    if resources.shutdown.draining:
        response.status_code = 503
        return ServingResponse(status="draining")
    return ServingResponse(status="serving")


@health_router.get(
    "/health/ready",
    response_model=ReadyResponse,
    responses={
        503: {
            "model": ReadyResponse,
            "description": "PostgreSQL или Redis недоступны либо процесс закрывается (слив трафика)",
        }
    },
    summary="Готов к работе: зависимости и миграции",
)
async def ready(resources: ResourcesDep, response: Response) -> ReadyResponse:
    if resources.shutdown.draining:
        response.status_code = 503
        return ReadyResponse(status="unavailable", checks={"shutdown": "draining"}, degraded={})
    report = await run_readiness(resources.engine, resources.redis, resources.expected_head)
    response.status_code = 200 if report.ready else 503
    return ReadyResponse(
        status="ready" if report.ready else "unavailable",
        checks=report.checks,
        degraded=report.degraded,
    )


async def _snapshot(resources: AppResources) -> Snapshot:
    """Состояние на момент опроса: пул соединений БД и глубина очередей arq.

    Недоступный Redis не мешает отдать остальное: очереди просто не попадают в ответ.
    """
    pool = resources.engine.sync_engine.pool
    in_use = pool.checkedout() if isinstance(pool, QueuePool) else 0
    depth: dict[str, int] = {}
    for queue in QUEUES:
        try:
            depth[queue] = int(await resources.redis.zcard(queue_key(queue)))  # pyright: ignore[reportGeneralTypeIssues, reportUnknownMemberType, reportUnknownArgumentType]
        except (RedisError, OSError, TimeoutError):
            get_logger("messunjerr.metrics").debug("queue_depth_unavailable", queue=queue)
    return Snapshot(
        pool_in_use=in_use, pool_size=resources.settings.db_pool_size, queue_depth=depth
    )


@health_router.get(
    "/metrics",
    include_in_schema=False,
    summary="Метрики Prometheus",
)
async def metrics(resources: ResourcesDep) -> Response:
    # Только из внутренней сети: Caddy отвечает на /metrics снаружи 404 (4.15).
    return Response(
        content=render_metrics(await _snapshot(resources)),
        media_type=METRICS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


@api_router.get("/meta", response_model=MetaResponse, summary="Параметры для клиента")
async def meta(resources: ResourcesDep, response: Response) -> MetaResponse:
    settings = resources.settings
    response.headers["Cache-Control"] = "public, max-age=300"
    return MetaResponse(
        version=__version__,
        build=settings.app_build,
        server_time=utcnow(),
        limits=limits_for_meta(settings),
        reactions=settings.reaction_palette,
        auth=MetaAuth(
            methods=settings.auth_methods,
            oauth_providers=[],
            password_min_length=PASSWORD_MIN_LENGTH,
            password_max_length=PASSWORD_MAX_LENGTH,
        ),
        legal=MetaLegal(
            min_age=settings.min_age,
            documents=[MetaLegalDocument(slug="terms", version=settings.legal_terms_version)],
        ),
        features=MetaFeatures(email_notifications=False, data_export=False),
    )

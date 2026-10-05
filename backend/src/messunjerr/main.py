"""Фабрика ASGI-приложения: `uvicorn messunjerr.main:create_app --factory`."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI

from messunjerr import __version__
from messunjerr.core.db import create_engine, create_sessionmaker
from messunjerr.core.deps import AppResources
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import configure_logging, get_logger
from messunjerr.core.middleware import RequestContextMiddleware, RequestGuardMiddleware
from messunjerr.core.migrations import expected_head
from messunjerr.core.openapi import install_openapi
from messunjerr.core.problems import install_problem_handlers
from messunjerr.core.redis import create_redis
from messunjerr.identity.api.routers import api_router as identity_api_router
from messunjerr.identity.api.routers import well_known_router
from messunjerr.identity.services import create_identity_services
from messunjerr.jobs.queue import ArqJobQueue
from messunjerr.settings import Settings, check_runtime, get_settings
from messunjerr.system import api_router, health_router

# Пути, где вместо JSON допустима форма (например, отписка по RFC 8058, S10).
NON_JSON_PATHS = frozenset({"/api/v1/notifications/email/unsubscribe"})

DESCRIPTION = """Бэкенд messunjerr v2: соцсеть с чатом.

Формат ошибок: RFC 9457 `application/problem+json`; каталог кодов в спецификации (5.14).
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings: Settings = app.state.settings
    log = get_logger("messunjerr.lifespan")
    engine = create_engine(settings)
    redis = create_redis(settings)
    # Тесты подставляют свою очередь; иначе работает arq (соединение с Redis создаётся лениво).
    injected: JobQueue | None = app.state.job_queue
    arq_queue = ArqJobQueue(settings.redis_url.get_secret_value()) if injected is None else None
    jobs: JobQueue = injected if injected is not None else cast(ArqJobQueue, arq_queue)
    identity = await create_identity_services(settings)
    app.state.identity = identity
    app.state.resources = AppResources(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        redis=redis,
        jobs=jobs,
        expected_head=expected_head(),
    )
    log.info("startup", env=settings.app_env, version=__version__, build=settings.app_build)
    try:
        yield
    finally:
        identity.close()
        if arq_queue is not None:
            await arq_queue.close()
        await redis.aclose()
        await engine.dispose()
        log.info("shutdown")


def create_app(settings: Settings | None = None, *, job_queue: JobQueue | None = None) -> FastAPI:
    settings = settings or get_settings()
    check_runtime(settings)
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="messunjerr API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/api/v1/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.job_queue = job_queue

    install_problem_handlers(app)
    # Порядок: последний добавленный стоит снаружи, поэтому контекст запроса оборачивает защиту.
    app.add_middleware(
        RequestGuardMiddleware,
        max_body_bytes=settings.request_body_limit_bytes,
        non_json_paths=NON_JSON_PATHS,
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health_router)
    app.include_router(well_known_router)
    app.include_router(api_router)
    app.include_router(identity_api_router)
    install_openapi(app)
    return app

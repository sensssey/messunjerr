"""Фабрика ASGI-приложения: `uvicorn messunjerr.main:create_app --factory`."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI

from messunjerr import __version__
from messunjerr.core.db import create_engine, create_sessionmaker
from messunjerr.core.deps import AppResources
from messunjerr.core.fields import warm_up_timezones
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import configure_logging, get_logger
from messunjerr.core.middleware import RequestContextMiddleware, RequestGuardMiddleware
from messunjerr.core.migrations import expected_head
from messunjerr.core.openapi import install_openapi
from messunjerr.core.problems import install_problem_handlers
from messunjerr.core.ratelimit import BucketConfig, RateLimiter, load_buckets
from messunjerr.core.redis import create_redis
from messunjerr.core.shutdown import ShutdownGate, get_shutdown_gate
from messunjerr.identity.api.routers import api_router as identity_api_router
from messunjerr.identity.api.routers import well_known_router
from messunjerr.identity.services import create_identity_services
from messunjerr.jobs.queue import ArqJobQueue
from messunjerr.media.api.routers import api_router as media_api_router
from messunjerr.media.commands.avatars import MediaAvatarAssets
from messunjerr.media.domain.ports import AssetAudience, ObjectStorage
from messunjerr.media.infra.usage import CompositeAssetUsage
from messunjerr.media.services import create_media_services
from messunjerr.profiles.api.routers import api_router as profiles_api_router
from messunjerr.profiles.infra.usage import ProfileAvatarUsage
from messunjerr.profiles.services import create_profile_services
from messunjerr.settings import Settings, check_runtime, get_settings
from messunjerr.spike import spike_router
from messunjerr.system import api_router, health_router

# Пути, где вместо JSON допустима форма (например, отписка по RFC 8058, S10).
NON_JSON_PATHS = frozenset({"/api/v1/notifications/email/unsubscribe"})

DESCRIPTION = """Бэкенд messunjerr v2: соцсеть с чатом.

Формат ошибок: RFC 9457 `application/problem+json`; каталог кодов в спецификации (5.14).
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings: Settings = app.state.settings
    gate: ShutdownGate = app.state.shutdown
    log = get_logger("messunjerr.lifespan")
    gate.attach()
    # База часовых поясов читается один раз при старте, а не на первом запросе с `timezone`.
    await asyncio.to_thread(warm_up_timezones)
    engine = create_engine(settings)
    redis = create_redis(settings)
    # Тесты подставляют свою очередь; иначе работает arq (соединение с Redis создаётся лениво).
    injected: JobQueue | None = app.state.job_queue
    arq_queue = ArqJobQueue(settings.redis_url.get_secret_value()) if injected is None else None
    jobs: JobQueue = injected if injected is not None else cast(ArqJobQueue, arq_queue)
    # Порты профилей: аватары реализует медиа, граф и контент пока заглушки (S7–S8, S11).
    profiles = create_profile_services(avatars=MediaAvatarAssets(jobs))
    # identity ниже profiles в графе контекстов (4.2): создание профиля при регистрации и разделы
    # `MeUser` ему приносят порты, которые реализуют профили.
    identity = await create_identity_services(
        settings, redis, me_extras=profiles, provisioner=profiles
    )
    # Хранилище файлов: S3 по настройкам либо подставное из тестов; к чему привязан ресурс, знают
    # контексты, и каждый приносит свою часть (аватар профиля, позже вложения постов и сообщений).
    media = create_media_services(
        settings,
        storage=app.state.storage,
        usage=CompositeAssetUsage([ProfileAvatarUsage()]),
        audience=app.state.asset_audience,
    )
    await media.storage.warm_up()
    app.state.identity = identity
    app.state.profiles = profiles
    app.state.media = media
    app.state.resources = AppResources(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        redis=redis,
        jobs=jobs,
        limiter=RateLimiter(redis, app.state.rate_limits, enabled=settings.rate_limits_enabled),
        expected_head=expected_head(),
        shutdown=gate,
    )
    log.info("startup", env=settings.app_env, version=__version__, build=settings.app_build)
    try:
        yield
    finally:
        gate.detach()
        identity.close()
        await media.close()
        if arq_queue is not None:
            await arq_queue.close()
        await redis.aclose()
        await engine.dispose()
        log.info("shutdown")


def create_app(
    settings: Settings | None = None,
    *,
    job_queue: JobQueue | None = None,
    rate_limits: Mapping[str, BucketConfig] | None = None,
    shutdown_gate: ShutdownGate | None = None,
    storage: ObjectStorage | None = None,
    asset_audience: AssetAudience | None = None,
) -> FastAPI:
    """Собирает приложение. `job_queue`, `rate_limits`, `shutdown_gate`, `storage` и `asset_audience`
    подставляют тесты.

    Иначе работают arq, ratelimits.toml, калитка процесса, которую закрывает `GracefulServer`, и
    клиент S3 по настройкам `S3_*` (без них ручки загрузки отвечают 503).
    """
    settings = settings or get_settings()
    check_runtime(settings, needs_storage=True)
    configure_logging(settings.log_level, settings.log_format)
    # Ошибка в файле лимитов останавливает старт, а не всплывает на первом запросе.
    buckets = (
        dict(rate_limits)
        if rate_limits is not None
        else load_buckets(Path(settings.rate_limits_file) if settings.rate_limits_file else None)
    )

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
    app.state.rate_limits = buckets
    app.state.shutdown = shutdown_gate or get_shutdown_gate()
    app.state.storage = storage
    app.state.asset_audience = asset_audience

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
    app.include_router(profiles_api_router)
    app.include_router(media_api_router)
    if settings.spike_endpoints_enabled:
        app.include_router(spike_router)
    install_openapi(app)
    return app

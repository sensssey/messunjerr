"""Служебные ручки (5.13): здоровье и параметры для клиента."""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from messunjerr import __version__
from messunjerr.core.clock import utcnow
from messunjerr.core.deps import ResourcesDep
from messunjerr.core.health import run_readiness
from messunjerr.core.limits import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, limits_for_meta
from messunjerr.core.schemas import UtcDateTime

# Снаружи эти адреса закрывает Caddy (404): они нужны только оркестратору и мониторингу.
health_router = APIRouter(tags=["service"])
api_router = APIRouter(prefix="/api/v1", tags=["service"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


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
    "/health/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "PostgreSQL или Redis недоступны"}},
    summary="Готов принимать трафик",
)
async def ready(resources: ResourcesDep, response: Response) -> ReadyResponse:
    report = await run_readiness(resources.engine, resources.redis, resources.expected_head)
    response.status_code = 200 if report.ready else 503
    return ReadyResponse(
        status="ready" if report.ready else "unavailable",
        checks=report.checks,
        degraded=report.degraded,
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

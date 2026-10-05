"""Зависимости FastAPI для лимитов запросов: проверка бакетов и заголовки `RateLimit-*` (5.1, 4.14)."""

import hashlib
import ipaddress
from collections.abc import Awaitable, Callable, Sequence

from fastapi import Request, Response

from messunjerr.core.deps import ResourcesDep
from messunjerr.core.ratelimit import (
    RateLimiter,
    RateLimitResult,
    most_restrictive,
    rate_limited,
)

UNKNOWN_CLIENT = "unknown"


def client_ip(request: Request) -> str:
    """Адрес клиента для лимитов. За Caddy его подставляет uvicorn из доверенного заголовка."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else UNKNOWN_CLIENT
    except ValueError:
        return UNKNOWN_CLIENT


def subject_digest(value: str) -> str:
    """Субъект лимита по строке (почта, логин): в ключах Redis лежит хэш, а не сам адрес (⚖️)."""
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:24]


RATELIMIT_STATE_KEY = "ratelimit_headers"
"""Ключ в `scope["state"]`: `RateLimit-*` запроса, чтобы их получили и ответы-ошибки (их строит
обработчик исключений, а не ручка)."""


async def enforce(
    limiter: RateLimiter,
    checks: Sequence[tuple[str, str]],
    response: Response | None = None,
    request: Request | None = None,
) -> RateLimitResult | None:
    """Проверяет бакеты по порядку; первый исчерпанный даёт `429`.

    При успехе заголовки `RateLimit-*` самого «тесного» из бакетов попадают в ответ ручки
    (`response`) и в состояние запроса (`request`), откуда их берут ответы-ошибки.
    """
    results: list[RateLimitResult] = []
    for bucket, subject in checks:
        result = await limiter.consume(bucket, subject)
        if not result.allowed:
            raise rate_limited(result)
        results.append(result)
    if not results:
        return None
    tightest = most_restrictive(results)
    if response is not None:
        response.headers.update(tightest.headers())
    if request is not None:
        request.scope.setdefault("state", {})[RATELIMIT_STATE_KEY] = tightest.headers()
    return tightest


def limit_by_ip(*buckets: str) -> Callable[..., Awaitable[None]]:
    """Зависимость: лимиты по адресу клиента (до разбора тела запроса)."""

    async def dependency(request: Request, response: Response, resources: ResourcesDep) -> None:
        ip = client_ip(request)
        await enforce(resources.limiter, [(bucket, ip) for bucket in buckets], response, request)

    return dependency

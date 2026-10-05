"""Зависимости FastAPI контекста identity: службы, клиент, текущий пользователь, лимиты по пользователю."""

import ipaddress
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep
from messunjerr.core.logs import get_logger
from messunjerr.core.ratelimit_deps import enforce
from messunjerr.identity.commands.common import ClientInfo
from messunjerr.identity.domain.errors import (
    account_deletion_pending,
    service_unavailable,
    unauthorized,
)
from messunjerr.identity.infra.jwt_service import AccessTokenError
from messunjerr.identity.infra.session_denylist import AccessState
from messunjerr.identity.services import IdentityServices


def get_identity(request: Request) -> IdentityServices:
    return cast(IdentityServices, request.app.state.identity)  # pyright: ignore[reportUnknownMemberType]


IdentityDep = Annotated[IdentityServices, Depends(get_identity)]


def get_client_info(request: Request) -> ClientInfo:
    """IP и User-Agent запроса. За Caddy адрес берётся из доверенного заголовка (настройка uvicorn)."""
    host = request.client.host if request.client else None
    try:
        ip = str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        ip = None
    return ClientInfo(ip=ip, user_agent=request.headers.get("user-agent"))


ClientDep = Annotated[ClientInfo, Depends(get_client_info)]


def no_store(response: Response) -> None:
    """`Cache-Control: no-store` на ручках аутентификации и личных данных (5.1)."""
    response.headers["Cache-Control"] = "no-store"


@dataclass(frozen=True, slots=True)
class Principal:
    """Кто делает запрос: данные берутся из проверенного access-токена, а не из БД."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    role: str
    token_id: str


bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Access-токен из `/auth/login` или `/auth/verify-email` (живёт 10 минут).",
)


async def _authenticate(
    credentials: HTTPAuthorizationCredentials | None,
    identity: IdentityServices,
    *,
    strict: bool,
    allow_deletion_pending: bool = False,
) -> Principal:
    """Подпись, срок, `iss`, `aud`, `scp`, denylist сессий и признак удаления аккаунта (4.7, 5.1).

    `strict` для чувствительных операций: без Redis отзыв сессии и признак удаления не проверить, и
    ответ `503`. Общие ручки в этом случае работают без этих проверок (с предупреждением в журнале).
    Аккаунт, который ждёт удаления, пускают только на `GET /me` и `POST /me/restore`
    (`allow_deletion_pending`); остальное даёт `403 account_deletion_pending`.
    """
    if credentials is None or not credentials.credentials.strip():
        raise unauthorized(ErrorCode.TOKEN_MISSING, "A bearer access token is required.")
    try:
        claims = identity.tokens.verify(credentials.credentials.strip())
    except AccessTokenError as error:
        raise unauthorized(error.code, error.detail) from error
    if claims.scope is not None:
        # В v1 ограниченных токенов (`scp = consent`, 4.7) нет: чужой `scp` не принимаем.
        raise unauthorized(ErrorCode.TOKEN_INVALID, "The token scope is not supported.")

    try:
        state = await identity.denylist.access_state(claims.session_id, claims.user_id)
    except (RedisError, OSError, TimeoutError) as error:
        if strict:
            raise service_unavailable("Session checks are temporarily unavailable.") from error
        get_logger("messunjerr.identity").warning("session_denylist_unavailable")
        state = AccessState()
    if state.revoked:
        raise unauthorized(ErrorCode.SESSION_REVOKED, "The session has been revoked.")
    if state.deletion_pending and not allow_deletion_pending:
        raise account_deletion_pending()
    return Principal(
        user_id=claims.user_id,
        session_id=claims.session_id,
        role=claims.role,
        token_id=claims.token_id,
    )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    identity: IdentityDep,
) -> Principal:
    return await _authenticate(credentials, identity, strict=False)


async def get_sensitive_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    identity: IdentityDep,
) -> Principal:
    """Для смены пароля и почты, удаления аккаунта, «выйти везде», сессий: при недоступном Redis `503`."""
    return await _authenticate(credentials, identity, strict=True)


async def get_principal_allowing_deletion(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    identity: IdentityDep,
) -> Principal:
    """Для `GET /me` и `POST /me/restore`: пускает и аккаунт, который ждёт удаления (5.1)."""
    return await _authenticate(credentials, identity, strict=False, allow_deletion_pending=True)


PrincipalDep = Annotated[Principal, Depends(get_principal)]
SensitivePrincipalDep = Annotated[Principal, Depends(get_sensitive_principal)]
PrincipalAllowingDeletionDep = Annotated[Principal, Depends(get_principal_allowing_deletion)]


async def principal_user_id(request: Request) -> uuid.UUID | None:
    """Пользователь по access-токену запроса или `None`. Нужен `Idempotency-Key` (5.1): ключ действует
    в пределах пользователя. Ошибки токена здесь не выдаются: их выдаст сама ручка."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    try:
        return get_identity(request).tokens.verify(value.strip()).user_id
    except AccessTokenError:
        return None


def limit_user(
    *buckets: str, sensitive: bool = False, allow_deletion_pending: bool = False
) -> Callable[..., Awaitable[None]]:
    """Зависимость: лимиты по пользователю (`api_read`, `api_write`) и заголовки `RateLimit-*`.

    Принимает те же виды допуска, что и сама ручка: `sensitive` (без Redis `503`) и
    `allow_deletion_pending` (аккаунт, который ждёт удаления, не отсекается).
    """
    if sensitive:

        async def sensitive_dependency(
            principal: SensitivePrincipalDep,
            request: Request,
            response: Response,
            resources: ResourcesDep,
        ) -> None:
            checks = [(bucket, str(principal.user_id)) for bucket in buckets]
            await enforce(resources.limiter, checks, response, request)

        return sensitive_dependency

    if allow_deletion_pending:

        async def tolerant_dependency(
            principal: PrincipalAllowingDeletionDep,
            request: Request,
            response: Response,
            resources: ResourcesDep,
        ) -> None:
            checks = [(bucket, str(principal.user_id)) for bucket in buckets]
            await enforce(resources.limiter, checks, response, request)

        return tolerant_dependency

    async def dependency(
        principal: PrincipalDep, request: Request, response: Response, resources: ResourcesDep
    ) -> None:
        checks = [(bucket, str(principal.user_id)) for bucket in buckets]
        await enforce(resources.limiter, checks, response, request)

    return dependency

"""Зависимости FastAPI контекста identity: службы, клиент, текущий пользователь."""

import ipaddress
import uuid
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep
from messunjerr.core.logs import get_logger
from messunjerr.identity.commands.common import ClientInfo
from messunjerr.identity.domain.errors import unauthorized
from messunjerr.identity.infra.jwt_service import AccessTokenError
from messunjerr.identity.services import IdentityServices

SESSION_REVOKED_KEY = "sess:revoked:{sid}"
"""Ключ denylist в Redis (4.13): `sid` отозванной сессии живёт столько же, сколько access-токен."""


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


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    identity: IdentityDep,
    resources: ResourcesDep,
) -> Principal:
    """Проверяет `Authorization: Bearer`: подпись, срок, `iss`, `aud`, `scp` и denylist сессий."""
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
        revoked = await resources.redis.exists(  # pyright: ignore[reportUnknownMemberType]
            SESSION_REVOKED_KEY.format(sid=claims.session_id)
        )
    except (RedisError, OSError, TimeoutError):
        # Общие ручки работают без denylist (4.7); чувствительные (S2) в этом случае отвечают 503.
        get_logger("messunjerr.identity").warning("session_denylist_unavailable")
        revoked = 0
    if revoked:
        raise unauthorized(ErrorCode.SESSION_REVOKED, "The session has been revoked.")
    return Principal(
        user_id=claims.user_id,
        session_id=claims.session_id,
        role=claims.role,
        token_id=claims.token_id,
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]

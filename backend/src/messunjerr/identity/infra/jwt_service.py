"""Access-токены: JWT с подписью Ed25519 (EdDSA), `kid` в заголовке и JWKS для проверки (4.7).

Claims: `iss`, `aud = messunjerr-api`, `sub` (пользователь), `sid` (сессия), `role`, `iat`, `exp`,
`jti`, необязательный `scp`. Статус пользователя в токен не кладётся: блокировка отзывает сессии и
добавляет `sid` в denylist Redis (S2).
"""

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode
from messunjerr.core.ids import uuid7
from messunjerr.core.logs import get_logger
from messunjerr.settings import Settings

AUDIENCE = "messunjerr-api"
ALGORITHM = "EdDSA"
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "sid", "jti"]
_SEED_LENGTH = 32


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _raw_public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_private_key(value: str) -> Ed25519PrivateKey:
    """Ключ из PEM (PKCS8) или из 32 байт seed в base64url (удобно для `.env` в разработке)."""
    text = value.strip()
    if text.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("JWT_PRIVATE_KEY: ожидается ключ Ed25519")
        return key
    seed = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(seed) != _SEED_LENGTH:
        raise ValueError("JWT_PRIVATE_KEY: seed Ed25519 должен занимать 32 байта (base64url)")
    return Ed25519PrivateKey.from_private_bytes(seed)


def key_thumbprint(key: Ed25519PublicKey) -> str:
    """Отпечаток открытого ключа по RFC 7638: стабильный `kid`, если имя не задано явно."""
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": _b64url(_raw_public_bytes(key))},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def public_jwk(key: Ed25519PublicKey, kid: str) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url(_raw_public_bytes(key)),
        "kid": kid,
        "use": "sig",
        "alg": ALGORITHM,
    }


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    role: str
    token_id: str
    scope: str | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    token: str
    expires_at: datetime
    expires_in: int


class AccessTokenError(Exception):
    """Токен не принят: `code` это `token_invalid` или `token_expired` из каталога 5.14."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class TokenService:
    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        key_id: str | None,
        issuer: str,
        ttl_seconds: int,
        retired_public_keys: Mapping[str, Ed25519PublicKey] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._private_key = private_key
        self._public_key = private_key.public_key()
        self.key_id = key_id or key_thumbprint(self._public_key)
        self._issuer = issuer
        self._ttl = timedelta(seconds=ttl_seconds)
        self._retired = dict(retired_public_keys or {})
        self._clock = clock
        # Проверяются и текущий ключ, и прежние: токены, выданные до ротации, живут до 20 минут.
        self._verification_keys: dict[str, Ed25519PublicKey] = {
            **self._retired,
            self.key_id: self._public_key,
        }

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        role: str,
        scope: str | None = None,
        now: datetime | None = None,
    ) -> IssuedAccessToken:
        issued_at = (now or self._clock()).astimezone(UTC)
        expires_at = issued_at + self._ttl
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": AUDIENCE,
            "sub": str(user_id),
            "sid": str(session_id),
            "role": role,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": uuid7().hex,
        }
        if scope is not None:
            claims["scp"] = scope
        token = jwt.encode(
            claims, self._private_key, algorithm=ALGORITHM, headers={"kid": self.key_id}
        )
        return IssuedAccessToken(token=token, expires_at=expires_at, expires_in=self.ttl_seconds)

    def verify(self, token: str) -> AccessClaims:
        """Проверяет подпись, срок, `iss`, `aud` и обязательные claims; ошибка: `AccessTokenError`."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AccessTokenError(
                ErrorCode.TOKEN_INVALID, "The access token is malformed."
            ) from error
        kid = header.get("kid")
        key = self._verification_keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise AccessTokenError(ErrorCode.TOKEN_INVALID, "The token signing key is unknown.")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=self._issuer,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as error:
            raise AccessTokenError(
                ErrorCode.TOKEN_EXPIRED, "The access token has expired."
            ) from error
        except jwt.PyJWTError as error:
            raise AccessTokenError(
                ErrorCode.TOKEN_INVALID, "The access token is invalid."
            ) from error
        return self._parse(claims)

    @staticmethod
    def _parse(claims: Mapping[str, Any]) -> AccessClaims:
        try:
            scope = claims.get("scp")
            return AccessClaims(
                user_id=uuid.UUID(str(claims["sub"])),
                session_id=uuid.UUID(str(claims["sid"])),
                role=str(claims.get("role", "user")),
                token_id=str(claims["jti"]),
                scope=str(scope) if scope is not None else None,
                issued_at=datetime.fromtimestamp(int(claims["iat"]), UTC),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise AccessTokenError(
                ErrorCode.TOKEN_INVALID, "The access token is invalid."
            ) from error

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """Открытые ключи для проверки подписи другими сервисами (`GET /.well-known/jwks.json`)."""
        keys = [public_jwk(key, kid) for kid, key in self._verification_keys.items()]
        return {"keys": keys}


def create_token_service(settings: Settings) -> TokenService:
    if settings.jwt_private_key is not None:
        private_key = load_private_key(settings.jwt_private_key.get_secret_value())
    else:
        # Только dev и test: в prod и stage `check_runtime` останавливает старт без ключа.
        private_key = Ed25519PrivateKey.generate()
        get_logger("messunjerr.identity").warning(
            "jwt_key_ephemeral",
            hint="JWT_PRIVATE_KEY не задан: ключ создан на время работы процесса",
        )
    return TokenService(
        private_key=private_key,
        key_id=settings.jwt_key_id,
        issuer=settings.jwt_issuer,
        ttl_seconds=settings.access_token_ttl_seconds,
    )

"""Access-токены: выдача, проверка, срок, подмена, ротация ключа, JWKS."""

import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from messunjerr.core.codes import ErrorCode
from messunjerr.identity.infra.jwt_service import (
    AUDIENCE,
    AccessTokenError,
    TokenService,
    create_token_service,
    key_thumbprint,
    load_private_key,
)
from messunjerr.settings import Settings

SEED = bytes(range(32))
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
USER_ID = uuid.UUID("0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80")
SESSION_ID = uuid.UUID("0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d81")


def service(key: Ed25519PrivateKey = KEY, **kwargs: Any) -> TokenService:
    options: dict[str, Any] = {"key_id": None, "issuer": "messunjerr", "ttl_seconds": 600}
    options.update(kwargs)
    return TokenService(private_key=key, **options)


def issued(svc: TokenService, **kwargs: Any) -> str:
    return svc.issue(user_id=USER_ID, session_id=SESSION_ID, role="user", **kwargs).token


def code_of(error: pytest.ExceptionInfo[AccessTokenError]) -> ErrorCode:
    return error.value.code


def test_roundtrip_returns_the_issued_claims() -> None:
    svc = service()
    result = svc.issue(user_id=USER_ID, session_id=SESSION_ID, role="moderator")
    claims = svc.verify(result.token)
    assert claims.user_id == USER_ID
    assert claims.session_id == SESSION_ID
    assert claims.role == "moderator"
    assert claims.scope is None
    assert len(claims.token_id) == 32
    assert claims.expires_at - claims.issued_at == timedelta(seconds=600)
    assert result.expires_in == 600


def test_header_carries_algorithm_and_kid() -> None:
    svc = service()
    header = jwt.get_unverified_header(issued(svc))
    assert header["alg"] == "EdDSA"
    assert header["kid"] == svc.key_id


def test_kid_defaults_to_the_key_thumbprint_and_can_be_named() -> None:
    assert service().key_id == key_thumbprint(KEY.public_key())
    assert service(key_id="2026-10").key_id == "2026-10"


def test_each_token_has_its_own_jti() -> None:
    svc = service()
    assert svc.verify(issued(svc)).token_id != svc.verify(issued(svc)).token_id


def test_scope_claim_is_parsed() -> None:
    svc = service()
    assert svc.verify(issued(svc, scope="consent")).scope == "consent"


def test_expired_token_is_token_expired() -> None:
    svc = service()
    token = issued(svc, now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(token)
    assert code_of(caught) is ErrorCode.TOKEN_EXPIRED


def test_token_issued_in_the_future_is_invalid() -> None:
    svc = service()
    token = issued(svc, now=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(token)
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_tampered_payload_is_invalid() -> None:
    svc = service()
    header, payload, signature = issued(svc).split(".")
    forged = base64.urlsafe_b64encode(
        base64.urlsafe_b64decode(payload + "==").replace(b'"user"', b'"admin"')
    ).rstrip(b"=")
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(f"{header}.{forged.decode()}.{signature}")
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_signature_by_another_key_is_invalid_even_with_the_same_kid() -> None:
    attacker = service(Ed25519PrivateKey.generate(), key_id="shared")
    victim = service(key_id="shared")
    with pytest.raises(AccessTokenError) as caught:
        victim.verify(issued(attacker))
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_unknown_kid_is_invalid() -> None:
    other = service(Ed25519PrivateKey.generate())
    with pytest.raises(AccessTokenError) as caught:
        service().verify(issued(other))
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "messunjerr",
        "aud": AUDIENCE,
        "sub": str(USER_ID),
        "sid": str(SESSION_ID),
        "role": "user",
        "iat": now,
        "exp": now + 600,
        "jti": "abc",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "someone-else"},
        {"aud": "another-api"},
        {"sid": None},
        {"sub": None},
        {"jti": None},
        {"exp": None},
        {"sub": "not-a-uuid"},
        {"sid": "not-a-uuid"},
    ],
    ids=["iss", "aud", "no-sid", "no-sub", "no-jti", "no-exp", "sub-not-uuid", "sid-not-uuid"],
)
def test_wrong_or_missing_claims_are_invalid(overrides: dict[str, Any]) -> None:
    svc = service()
    token = jwt.encode(_claims(**overrides), KEY, algorithm="EdDSA", headers={"kid": svc.key_id})
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(token)
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_symmetric_algorithm_confusion_is_rejected() -> None:
    svc = service()
    token = jwt.encode(
        _claims(),
        "shared-secret-of-sufficient-length-1234",
        algorithm="HS256",
        headers={"kid": svc.key_id},
    )
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(token)
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_unsigned_token_is_rejected() -> None:
    svc = service()
    no_key: Any = None  # алгоритм "none" ключа не требует
    token = jwt.encode(_claims(), no_key, algorithm="none", headers={"kid": svc.key_id})
    with pytest.raises(AccessTokenError) as caught:
        svc.verify(token)
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "....", "Bearer x"])
def test_garbage_is_invalid(garbage: str) -> None:
    with pytest.raises(AccessTokenError) as caught:
        service().verify(garbage)
    assert code_of(caught) is ErrorCode.TOKEN_INVALID


def test_token_signed_before_key_rotation_still_verifies() -> None:
    old = service(key_id="old")
    token = issued(old)
    new_key = Ed25519PrivateKey.generate()
    rotated = service(new_key, key_id="new", retired_public_keys={"old": KEY.public_key()})
    assert rotated.verify(token).user_id == USER_ID
    assert {key["kid"] for key in rotated.jwks()["keys"]} == {"old", "new"}
    with pytest.raises(AccessTokenError):
        service(new_key, key_id="new").verify(token)  # без прежнего ключа токен недействителен


def test_jwks_publishes_only_the_public_key() -> None:
    svc = service(key_id="k1")
    (jwk,) = svc.jwks()["keys"]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert jwk["kid"] == "k1"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "EdDSA"
    assert "d" not in jwk
    assert len(base64.urlsafe_b64decode(jwk["x"] + "==")) == 32


def test_published_key_verifies_tokens_with_a_standard_client() -> None:
    svc = service()
    (jwk,) = svc.jwks()["keys"]
    public_key = jwt.PyJWK(jwk).key
    decoded = jwt.decode(issued(svc), public_key, algorithms=["EdDSA"], audience=AUDIENCE)
    assert decoded["sub"] == str(USER_ID)


# ----------------------------------------------------------------------------- загрузка ключа
def test_key_from_base64url_seed_with_and_without_padding() -> None:
    seed = base64.urlsafe_b64encode(SEED).decode()
    for value in (seed, seed.rstrip("="), f"  {seed}\n"):
        loaded = load_private_key(value)
        assert loaded.public_key().public_bytes_raw() == KEY.public_key().public_bytes_raw()


def test_key_from_pem() -> None:
    pem = KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert (
        load_private_key(pem).public_key().public_bytes_raw() == KEY.public_key().public_bytes_raw()
    )


def test_wrong_keys_are_rejected() -> None:
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="Ed25519"):
        load_private_key(rsa_pem)
    with pytest.raises(ValueError, match="32"):
        load_private_key(base64.urlsafe_b64encode(b"too short").decode())
    with pytest.raises(ValueError):  # noqa: PT011 (текст ошибки декодера не наш)
        load_private_key("!!! not a key !!!")


def _settings(**overrides: Any) -> Settings:
    options: dict[str, Any] = {
        "database_url": SecretStr("postgresql+asyncpg://app:p@h/db"),
        "redis_url": SecretStr("redis://h/0"),
        # Явный None перекрывает JWT_PRIVATE_KEY из окружения контейнера разработки.
        "jwt_private_key": None,
        "jwt_key_id": None,
    }
    options.update(overrides)
    return Settings(**options)  # pyright: ignore[reportCallIssue]


def test_service_uses_the_configured_key_and_ttl() -> None:
    svc = create_token_service(
        _settings(
            jwt_private_key=SecretStr(base64.urlsafe_b64encode(SEED).decode()),
            jwt_key_id="named",
            access_token_ttl_seconds=900,
        )
    )
    assert svc.key_id == "named"
    assert svc.ttl_seconds == 900
    assert svc.verify(issued(svc)).user_id == USER_ID


def test_without_a_key_the_service_gets_an_ephemeral_one() -> None:
    first = create_token_service(_settings())
    second = create_token_service(_settings())
    assert first.key_id != second.key_id

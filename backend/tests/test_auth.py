import time

import pytest
from jose import jwt

from src.auth.auth import get_password_hash, verify_password
from src.config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from src.database import SessionLocal
from src.models.models import UserDB

# Хэши, созданные прежней версией кода (passlib 1.7.4 + bcrypt): старые пользователи не должны потерять доступ
LEGACY_PASSWORD = "legacy-password-1"
LEGACY_HASH = "$2b$12$vtxHnWVw/mmhjofb6B.inuI.cNolEJlnnHyx2YjUcQgS6zH4Y21rG"
# passlib молча обрезал пароль до 72 байт
LEGACY_LONG_PASSWORD = "long-" + "x" * 95
LEGACY_LONG_HASH = "$2b$12$XYcdBUyRE1G13irevzc/LuAk8SVO3R5eznEvsAcIuK0rtHMbHeYVu"


def token_ttl_minutes(token):
    claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return (claims["exp"] - time.time()) / 60


async def test_register_returns_working_token(client):
    response = await client.post("/auth/register", json={"username": "alice", "password": "password-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"

    me = await client.get("/auth/users/me", headers={"Authorization": "Bearer " + body["access_token"]})
    assert me.status_code == 200
    assert me.json() == {"id": 1, "username": "alice", "avatar_url": None}


async def test_register_and_login_tokens_live_equally_long(client):
    registered = await client.post("/auth/register", json={"username": "alice", "password": "password-1"})
    logged_in = await client.post("/auth/token", data={"username": "alice", "password": "password-1"})
    assert logged_in.status_code == 200
    for token in (registered.json()["access_token"], logged_in.json()["access_token"]):
        assert token_ttl_minutes(token) == pytest.approx(ACCESS_TOKEN_EXPIRE_MINUTES, abs=1)


async def test_trailing_slash_variants_do_not_redirect(client):
    registered = await client.post("/auth/register/", json={"username": "alice", "password": "password-1"})
    assert registered.status_code == 200
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    assert (await client.get("/auth/users/me/", headers=headers)).status_code == 200


async def test_register_rejects_duplicate_username_ignoring_case(client, make_user):
    await make_user("alice")
    for username in ("alice", "ALICE"):
        response = await client.post("/auth/register", json={"username": username, "password": "password-1"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Username already registered"


@pytest.mark.parametrize("payload", [
    {"username": "", "password": "password-1"},
    {"username": "ab", "password": "password-1"},
    {"username": "a" * 33, "password": "password-1"},
    {"username": "has space", "password": "password-1"},
    {"username": "alice", "password": ""},
    {"username": "alice", "password": "short"},
    {"username": "alice", "password": "x" * 73},
    {"username": "alice", "password": "я" * 40},  # 40 символов, но 80 байт
])
async def test_register_validates_input(client, payload):
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 422
    # фронтенд показывает ошибки типа value_error
    assert response.json()["detail"][0]["type"] == "value_error"


async def test_register_trims_username(client):
    response = await client.post("/auth/register", json={"username": "  alice  ", "password": "password-1"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    me = await client.get("/auth/users/me", headers={"Authorization": "Bearer " + token})
    assert me.json()["username"] == "alice"


async def test_login_does_not_reveal_whether_user_exists(client, make_user):
    await make_user("alice")
    wrong_password = await client.post("/auth/token", data={"username": "alice", "password": "nope"})
    unknown_user = await client.post("/auth/token", data={"username": "ghost", "password": "nope"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer garbage"}])
async def test_protected_endpoint_requires_valid_token(client, headers):
    assert (await client.get("/auth/users/me", headers=headers)).status_code == 401


async def test_expired_token_is_rejected(client, make_user):
    await make_user("alice")
    expired = jwt.encode({"sub": "alice", "exp": int(time.time()) - 10}, SECRET_KEY, algorithm=ALGORITHM)
    response = await client.get("/auth/users/me", headers={"Authorization": "Bearer " + expired})
    assert response.status_code == 401


async def test_token_of_unknown_user_is_rejected(client):
    token = jwt.encode({"sub": "ghost", "exp": int(time.time()) + 60}, SECRET_KEY, algorithm=ALGORITHM)
    response = await client.get("/auth/users/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401


def test_password_hash_is_salted_bcrypt():
    first, second = get_password_hash("password-1"), get_password_hash("password-1")
    assert first != second
    assert first.startswith("$2b$")
    assert verify_password("password-1", first)
    assert not verify_password("password-2", first)


def test_verify_password_tolerates_garbage_hash():
    assert verify_password("password-1", "not-a-bcrypt-hash") is False


def test_hashes_from_previous_version_still_verify():
    assert verify_password(LEGACY_PASSWORD, LEGACY_HASH)
    assert not verify_password("wrong-password", LEGACY_HASH)
    assert verify_password(LEGACY_LONG_PASSWORD, LEGACY_LONG_HASH)


async def test_user_with_legacy_hash_can_log_in(client):
    async with SessionLocal() as session:
        session.add(UserDB(username="olduser", hashed_password=LEGACY_HASH))
        await session.commit()
    response = await client.post("/auth/token", data={"username": "olduser", "password": LEGACY_PASSWORD})
    assert response.status_code == 200

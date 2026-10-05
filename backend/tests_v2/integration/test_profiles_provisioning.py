"""S3-01: профиль и настройки приватности создаются вместе с аккаунтом, миграция 0003 и ограничения БД."""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .conftest import DatabaseUnderTest, create_database, drop_database, run_alembic
from .helpers import (
    execute,
    fetch_all,
    fetch_one,
    new_credentials,
    register,
    verification_token,
    verified_user,
)

DEFAULT_PRIVACY = {
    "dm_policy": "friends",
    "comment_policy": "everyone",
    "mention_policy": "everyone",
    "friends_list_visibility": "friends",
    "followers_list_visibility": "friends",
    "presence_visibility": "friends",
    "default_post_visibility": "friends",
}


async def profile_of(engine: AsyncEngine, username: str) -> dict[str, Any]:
    return await fetch_one(
        engine,
        "SELECT p.* FROM profile.profiles p JOIN identity.users u ON u.id = p.user_id "
        "WHERE u.username = :u",
        u=username,
    )


# ----------------------------------------------------------------------------- регистрация
async def test_registration_creates_a_default_profile_and_default_privacy(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    response, body = await register(client)
    assert response.status_code == 201, response.text

    profile = await profile_of(admin_engine, body["username"])
    assert profile["display_name"] == body["username"]  # имя по умолчанию равно нику
    assert profile["bio"] is None
    assert profile["links"] == []
    assert profile["birth_date"] is None
    assert profile["birth_date_visibility"] == "hidden"
    assert (profile["city"], profile["language"], profile["timezone"]) == (None, None, None)
    assert profile["is_private"] is False
    assert profile["avatar_asset_id"] is None
    privacy = await fetch_one(admin_engine, "SELECT * FROM profile.privacy_settings")
    assert {key: privacy[key] for key in DEFAULT_PRIVACY} == DEFAULT_PRIVACY
    (account,) = await fetch_all(admin_engine, "SELECT id FROM identity.users")
    assert account["id"] == profile["user_id"] == privacy["user_id"]


async def test_registration_stores_the_optional_profile_fields(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    response, body = await register(
        client, display_name="  Иван Петров ", language="en-us", timezone="Europe/Moscow"
    )
    assert response.status_code == 201, response.text

    profile = await profile_of(admin_engine, body["username"])

    assert profile["display_name"] == "Иван Петров"
    assert profile["language"] == "en-US"
    assert profile["timezone"] == "Europe/Moscow"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("display_name", "", "string_too_short"),
        ("display_name", "я" * 51, "string_too_long"),
        ("language", "klingon", "invalid_format"),
        ("timezone", "Moscow", "invalid_format"),
    ],
)
async def test_invalid_profile_fields_fail_registration_without_side_effects(
    client: httpx.AsyncClient,
    admin_engine: AsyncEngine,
    jobs: InMemoryJobQueue,
    field: str,
    value: str,
    code: str,
) -> None:
    response, _ = await register(client, **{field: value})

    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert (error["pointer"], error["code"]) == (f"/body/{field}", code)
    assert await fetch_all(admin_engine, "SELECT 1 FROM identity.users") == []
    assert await fetch_all(admin_engine, "SELECT 1 FROM profile.profiles") == []
    assert jobs.named("send_email") == []


async def test_a_repeated_registration_of_a_pending_address_replaces_the_profile_seed(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    """Тот, кто первым занял чужую почту, не оставляет за собой ни пароль, ни имя (pre-hijacking)."""
    first, squatter = await register(client, display_name="Захватчик", language="de")
    assert first.status_code == 201
    owner = new_credentials(email=squatter["email"], display_name="Настоящий владелец")

    second = await client.post("/api/v1/auth/register", json=owner)
    assert second.status_code == 201

    rows = await fetch_all(admin_engine, "SELECT display_name, language FROM profile.profiles")
    assert rows == [{"display_name": "Настоящий владелец", "language": None}]
    assert await fetch_all(admin_engine, "SELECT 1 FROM profile.privacy_settings") != []


async def test_a_repeated_registration_of_an_active_address_leaves_the_profile_alone(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs, display_name="Иван")
    before = await profile_of(admin_engine, user.credentials["username"])

    again = await client.post(
        "/api/v1/auth/register",
        json=new_credentials(email=user.credentials["email"], display_name="Чужой"),
    )

    assert again.status_code == 201
    assert await profile_of(admin_engine, user.credentials["username"]) == before
    assert len(await fetch_all(admin_engine, "SELECT 1 FROM profile.profiles")) == 1


async def test_a_failed_registration_rolls_the_profile_back(
    client: httpx.AsyncClient,
    admin_engine: AsyncEngine,
    jobs: InMemoryJobQueue,
) -> None:
    jobs.fail_with = ConnectionError("redis is down")

    response, _ = await register(client)

    assert response.status_code == 500
    assert await fetch_all(admin_engine, "SELECT 1 FROM identity.users") == []
    assert await fetch_all(admin_engine, "SELECT 1 FROM profile.profiles") == []
    assert await fetch_all(admin_engine, "SELECT 1 FROM profile.privacy_settings") == []


async def test_profile_rows_disappear_with_the_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    await verified_user(client, jobs)
    await verified_user(client, jobs)
    assert len(await fetch_all(admin_engine, "SELECT 1 FROM profile.profiles")) == 2

    await execute(
        admin_engine,
        "DELETE FROM identity.users WHERE id = (SELECT id FROM identity.users ORDER BY id LIMIT 1)",
    )

    assert len(await fetch_all(admin_engine, "SELECT 1 FROM profile.profiles")) == 1
    assert len(await fetch_all(admin_engine, "SELECT 1 FROM profile.privacy_settings")) == 1


async def test_verifying_the_email_keeps_the_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    _, body = await register(client, display_name="До подтверждения")
    before = await profile_of(admin_engine, body["username"])

    token = verification_token(jobs, body["email"])
    verify = await client.post("/api/v1/auth/verify-email", json={"token": token})

    assert verify.status_code == 200
    assert await profile_of(admin_engine, body["username"]) == before


# ----------------------------------------------------------------------------- ограничения БД
@pytest.mark.parametrize(
    "assignment",
    [
        "display_name = ''",
        "display_name = repeat('я', 51)",
        "bio = repeat('я', 501)",
        "links = '{}'::jsonb",
        "links = '\"x\"'::jsonb",
        "links = '[1,2,3,4,5,6]'::jsonb",
        "birth_date_visibility = 'everyone'",
        "city = repeat('я', 101)",
        "language = 'Russian'",
        "language = 'RU'",
        "language = 'r'",
    ],
)
async def test_the_database_rejects_invalid_profiles_even_without_the_app(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, assignment: str
) -> None:
    await verified_user(client, jobs)

    with pytest.raises(IntegrityError, match="violates check constraint"):
        await execute(admin_engine, f"UPDATE profile.profiles SET {assignment}")


@pytest.mark.parametrize(
    "column",
    [
        "dm_policy",
        "comment_policy",
        "mention_policy",
        "friends_list_visibility",
        "followers_list_visibility",
        "presence_visibility",
        "default_post_visibility",
    ],
)
async def test_the_database_rejects_unknown_privacy_values(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, column: str
) -> None:
    await verified_user(client, jobs)

    with pytest.raises(IntegrityError, match="violates check constraint"):
        await execute(admin_engine, f"UPDATE profile.privacy_settings SET {column} = 'bogus'")


async def test_a_profile_cannot_exist_without_an_account(admin_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="violates foreign key constraint"):
        await execute(
            admin_engine,
            "INSERT INTO profile.profiles (user_id, display_name) VALUES (:id, 'x')",
            id=uuid.uuid4(),
        )


async def test_the_readonly_role_can_read_profiles_but_not_write(admin_engine: AsyncEngine) -> None:
    async with admin_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE readonly"))
        count = (
            await connection.execute(text("SELECT count(*) FROM profile.profiles"))
        ).scalar_one()
    assert count == 0
    with pytest.raises(DBAPIError, match="permission denied"):
        await delete_profiles_as_readonly(admin_engine)


async def delete_profiles_as_readonly(admin_engine: AsyncEngine) -> None:
    async with admin_engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE readonly"))
        await connection.execute(text("DELETE FROM profile.profiles"))


# ----------------------------------------------------------------------------- миграция 0003
def _admin(target: DatabaseUnderTest) -> AsyncEngine:
    return create_async_engine(target.admin_url, poolclass=NullPool)


async def test_existing_accounts_get_a_profile_when_the_migration_runs(
    base_settings: Settings,
) -> None:
    """Аккаунты, созданные кодом S1 и S2, получают профиль и приватность по умолчанию (расширить, мигрировать)."""
    target = await create_database(base_settings, migrate=False)
    try:
        up = await run_alembic(["upgrade", "0002"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
        engine = _admin(target)
        try:
            for name in ("old_one", "old_two", "x" * 30):
                await execute(
                    engine,
                    "INSERT INTO identity.users (email, username, terms_version, terms_accepted_at, status) "
                    "VALUES (:e, :u, 'v', now(), 'active')",
                    e=f"{name[:8]}@example.com",
                    u=name,
                )
            head = await run_alembic(["upgrade", "head"], target.migrator_url)
            assert head.returncode == 0, head.stdout + head.stderr

            rows = await fetch_all(
                engine,
                "SELECT u.username::text AS username, p.display_name, p.is_private, "
                "s.dm_policy FROM identity.users u "
                "JOIN profile.profiles p ON p.user_id = u.id "
                "JOIN profile.privacy_settings s ON s.user_id = u.id ORDER BY u.username",
            )
        finally:
            await engine.dispose()
        assert [row["username"] for row in rows] == ["old_one", "old_two", "x" * 30]
        assert all(row["display_name"] == row["username"] for row in rows)
        assert all(row["is_private"] is False and row["dm_policy"] == "friends" for row in rows)
    finally:
        await drop_database(base_settings, target.name)


async def test_downgrading_drops_the_profile_tables_and_keeps_the_accounts(
    base_settings: Settings,
) -> None:
    target = await create_database(base_settings)
    try:
        engine = _admin(target)
        try:
            await execute(
                engine,
                "INSERT INTO identity.users (email, username, terms_version, terms_accepted_at) "
                "VALUES ('a@example.com', 'alpha', 'v', now())",
            )
            down = await run_alembic(["downgrade", "0002"], target.migrator_url)
            assert down.returncode == 0, down.stdout + down.stderr
            leftovers = await fetch_all(
                engine,
                "SELECT schemaname, tablename FROM pg_tables WHERE tablename IN "
                "('profiles', 'privacy_settings', 'username_reservations')",
            )
            users = await fetch_all(engine, "SELECT username::text AS u FROM identity.users")
            up = await run_alembic(["upgrade", "head"], target.migrator_url)
            assert up.returncode == 0, up.stdout + up.stderr
            restored = await fetch_all(engine, "SELECT display_name FROM profile.profiles")
        finally:
            await engine.dispose()
        assert leftovers == []
        assert users == [{"u": "alpha"}]
        assert restored == [{"display_name": "alpha"}]  # накат снова создаёт профили всем
    finally:
        await drop_database(base_settings, target.name)

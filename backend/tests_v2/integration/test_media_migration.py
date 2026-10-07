"""Миграция 0004 (S5): таблица `media.assets`, её ограничения и права, внешний ключ аватара профиля."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.settings import Settings

from .conftest import DatabaseUnderTest, create_database, drop_database, run_alembic
from .helpers import execute, fetch_all, fetch_one, verified_user

ASSET_COLUMNS = {
    "id", "owner_id", "kind", "purpose", "status", "object_key", "original_filename",
    "content_type", "declared_size", "size_bytes", "sha256", "width", "height", "variants",
    "reject_reason", "created_at", "uploaded_at", "processed_at", "deleted_at", "objects_deleted_at",
}  # fmt: skip
INSUFFICIENT_PRIVILEGE = "42501"


def asset_values(owner: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "owner": uuid.UUID(owner),
        "kind": "image",
        "purpose": "post",
        "status": "pending",
        "key": f"uploads/{uuid.uuid4()}/original",
        "size": 1000,
    }
    values.update(overrides)
    return values


INSERT = (
    "INSERT INTO media.assets (owner_id, kind, purpose, status, object_key, declared_size) "
    "VALUES (:owner, :kind, :purpose, :status, :key, :size)"
)


async def test_the_table_has_the_columns_of_the_specification_plus_the_cleanup_marks(
    admin_engine: AsyncEngine,
) -> None:
    rows = await fetch_all(
        admin_engine,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'media' AND table_name = 'assets'",
    )
    assert {row["column_name"] for row in rows} == ASSET_COLUMNS


async def test_indexes_serve_the_owner_listing_and_the_scheduled_jobs(
    admin_engine: AsyncEngine,
) -> None:
    rows = await fetch_all(
        admin_engine,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'media' AND tablename = 'assets'",
    )
    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert {
        "pk_assets", "uq_assets_object_key", "ix_assets_owner", "ix_assets_cleanup",
        "ix_assets_reconcile", "ix_assets_objects_pending",
    } <= set(definitions)  # fmt: skip
    assert "'pending'" in definitions["ix_assets_cleanup"]
    assert "objects_deleted_at IS NULL" in definitions["ix_assets_objects_pending"]


async def test_the_database_refuses_nonsense_values(
    client: Any, jobs: Any, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    for overrides in (
        {"kind": "video"},
        {"purpose": "wallpaper"},
        {"status": "frozen"},
        {"size": 0},
        {"size": -1},
    ):
        with pytest.raises(IntegrityError):
            await execute(admin_engine, INSERT, **asset_values(user.user_id, **overrides))
    with pytest.raises(IntegrityError):  # отрицательный размер готового файла
        await execute(
            admin_engine,
            "INSERT INTO media.assets (owner_id, kind, purpose, object_key, declared_size, size_bytes) "
            "VALUES (:owner, 'file', 'post', :key, 1, -1)",
            owner=uuid.UUID(user.user_id),
            key=f"uploads/{uuid.uuid4()}/original",
        )


async def test_an_object_key_belongs_to_one_asset(
    client: Any, jobs: Any, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    values = asset_values(user.user_id)
    await execute(admin_engine, INSERT, **values)
    with pytest.raises(IntegrityError):
        await execute(admin_engine, INSERT, **values)


async def test_the_app_role_works_with_assets_and_the_readonly_role_only_reads(
    client: Any, jobs: Any, engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    async with engine.begin() as connection:  # роль app: полный DML
        await connection.execute(text(INSERT), asset_values(user.user_id))
        await connection.execute(text("UPDATE media.assets SET status = 'uploaded'"))
        assert (
            await connection.execute(text("SELECT count(*) FROM media.assets"))
        ).scalar_one() == 1
        await connection.execute(text("DELETE FROM media.assets"))

    async def as_readonly(statement: str) -> None:
        async with admin_engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE readonly"))
            await connection.execute(text(statement))

    await as_readonly("SELECT * FROM media.assets")
    with pytest.raises(ProgrammingError) as caught:
        await as_readonly("DELETE FROM media.assets")
    assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE


async def test_the_avatar_of_a_profile_must_point_at_an_existing_asset(
    client: Any, jobs: Any, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    with pytest.raises(IntegrityError):
        await execute(
            admin_engine,
            "UPDATE profile.profiles SET avatar_asset_id = :asset WHERE user_id = :user",
            asset=uuid.uuid4(),
            user=uuid.UUID(user.user_id),
        )
    foreign_keys = await fetch_all(
        admin_engine,
        "SELECT confdeltype::text AS confdeltype FROM pg_constraint "
        "WHERE conname = 'fk_profiles_avatar_asset_id_assets' AND contype = 'f'",
    )
    assert [row["confdeltype"] for row in foreign_keys] == ["n"]  # ON DELETE SET NULL


# ----------------------------------------------------------------------------- путь миграции
def _admin(target: DatabaseUnderTest) -> AsyncEngine:
    return create_async_engine(target.admin_url, poolclass=NullPool)


async def test_upgrading_clears_stale_avatar_values_and_adds_the_key(
    base_settings: Settings,
) -> None:
    """Столбец `avatar_asset_id` существовал с 0003 без ключа: осиротевшие значения не должны ронять миграцию."""
    target = await create_database(base_settings, migrate=False)
    admin = _admin(target)
    try:
        up = await run_alembic(["upgrade", "0003"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
        user_id = uuid.uuid4()
        await execute(
            admin,
            "INSERT INTO identity.users (id, email, username, terms_version, terms_accepted_at, status) "
            "VALUES (:id, 'a@example.com', 'avatar_user', 'v', now(), 'active')",
            id=user_id,
        )
        await execute(  # профили создаёт код регистрации, а не БД: строку кладём сами
            admin,
            "INSERT INTO profile.profiles (user_id, display_name, avatar_asset_id) "
            "VALUES (:id, 'avatar_user', :asset)",
            id=user_id,
            asset=uuid.uuid4(),
        )

        head = await run_alembic(["upgrade", "head"], target.migrator_url)

        assert head.returncode == 0, head.stdout + head.stderr
        profile = await fetch_one(
            admin, "SELECT avatar_asset_id FROM profile.profiles WHERE user_id = :id", id=user_id
        )
        assert profile["avatar_asset_id"] is None
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)


async def test_downgrade_removes_the_table_and_the_key_and_upgrade_restores_them(
    base_settings: Settings,
) -> None:
    target = await create_database(base_settings)
    admin = _admin(target)
    try:
        down = await run_alembic(["downgrade", "0003"], target.migrator_url)
        assert down.returncode == 0, down.stdout + down.stderr
        tables = await fetch_all(
            admin, "SELECT 1 FROM pg_tables WHERE schemaname = 'media' AND tablename = 'assets'"
        )
        assert tables == []
        keys = await fetch_all(
            admin,
            "SELECT 1 FROM pg_constraint WHERE conname = 'fk_profiles_avatar_asset_id_assets'",
        )
        assert keys == []
        up = await run_alembic(["upgrade", "head"], target.migrator_url)
        assert up.returncode == 0, up.stdout + up.stderr
    finally:
        await admin.dispose()
        await drop_database(base_settings, target.name)

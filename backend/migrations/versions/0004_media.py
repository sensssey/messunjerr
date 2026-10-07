"""media: ресурсы (файлы) и внешний ключ аватара профиля на них

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEDIA = "media"
PROFILE = "profile"
IDENTITY = "identity"


def upgrade() -> None:
    # Права `app` и `readonly` на новую таблицу выдают права по умолчанию из миграции 0001.
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.LargeBinary(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "variants",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("objects_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.UniqueConstraint("object_key", name="uq_assets_object_key"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            [f"{IDENTITY}.users.id"],
            name="fk_assets_owner_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("kind IN ('image', 'file')", name=op.f("ck_assets_kind")),
        sa.CheckConstraint(
            "purpose IN ('avatar', 'group_avatar', 'post', 'message')",
            name=op.f("ck_assets_purpose"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'uploaded', 'processing', 'ready', 'rejected', 'deleted')",
            name=op.f("ck_assets_status"),
        ),
        sa.CheckConstraint("declared_size > 0", name=op.f("ck_assets_declared_size_positive")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_assets_size_bytes_not_negative")),
        schema=MEDIA,
    )
    op.create_index("ix_assets_owner", "assets", ["owner_id", "created_at"], schema=MEDIA)
    op.create_index(
        "ix_assets_cleanup",
        "assets",
        ["created_at"],
        schema=MEDIA,
        postgresql_where=sa.text("status IN ('pending','uploaded')"),
    )
    op.create_index(
        "ix_assets_reconcile",
        "assets",
        ["uploaded_at"],
        schema=MEDIA,
        postgresql_where=sa.text("status IN ('uploaded','processing')"),
    )
    op.create_index(
        "ix_assets_objects_pending",
        "assets",
        ["created_at"],
        schema=MEDIA,
        postgresql_where=sa.text("status IN ('deleted','rejected') AND objects_deleted_at IS NULL"),
    )

    # Выкладка без простоя (4.16): миграция не должна надолго перекрывать запись в профили. Поэтому
    # ожидание блокировки ограничено, а ключ создаётся без проверки существующих строк и проверяется
    # отдельно: обычный `ADD CONSTRAINT` держит `SHARE ROW EXCLUSIVE` всё время сканирования таблицы,
    # а `VALIDATE CONSTRAINT` берёт `SHARE UPDATE EXCLUSIVE` и записи не мешает.
    op.execute("SET LOCAL lock_timeout = '10s'")
    # Аватары назначали только заглушкой, которая отвечала «ресурса нет» (S3): значений в столбце быть
    # не должно, но ключ не должен упасть на случайной записи, поэтому осиротевшие значения обнуляются.
    op.execute(
        f"UPDATE {PROFILE}.profiles SET avatar_asset_id = NULL WHERE avatar_asset_id IS NOT NULL"
    )
    op.create_foreign_key(
        "fk_profiles_avatar_asset_id_assets",
        "profiles",
        "assets",
        ["avatar_asset_id"],
        ["id"],
        source_schema=PROFILE,
        referent_schema=MEDIA,
        ondelete="SET NULL",
        postgresql_not_valid=True,
    )
    op.execute(
        f"ALTER TABLE {PROFILE}.profiles VALIDATE CONSTRAINT fk_profiles_avatar_asset_id_assets"
    )


def downgrade() -> None:
    op.drop_constraint("fk_profiles_avatar_asset_id_assets", "profiles", schema=PROFILE)
    op.drop_table("assets", schema=MEDIA)

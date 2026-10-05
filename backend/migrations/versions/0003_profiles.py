"""profile: профили и настройки приватности, резервы прежних ников

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROFILE = "profile"
IDENTITY = "identity"


def upgrade() -> None:
    # Права `app` и `readonly` на новые таблицы выдают права по умолчанию из миграции 0001.
    op.create_table(
        "profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column(
            "links",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column(
            "birth_date_visibility", sa.Text(), server_default=sa.text("'hidden'"), nullable=False
        ),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # FK на media.assets добавит S6, когда появится таблица (S5)
        sa.Column("avatar_asset_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_profiles"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{IDENTITY}.users.id"],
            name="fk_profiles_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 50",
            name=op.f("ck_profiles_display_name_length"),
        ),
        sa.CheckConstraint("char_length(bio) <= 500", name=op.f("ck_profiles_bio_length")),
        sa.CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 5",
            name=op.f("ck_profiles_links_shape"),
        ),
        sa.CheckConstraint(
            "birth_date_visibility IN ('hidden', 'day_month', 'full')",
            name=op.f("ck_profiles_birth_date_visibility"),
        ),
        sa.CheckConstraint("char_length(city) <= 100", name=op.f("ck_profiles_city_length")),
        sa.CheckConstraint(
            "language ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'",
            name=op.f("ck_profiles_language_format"),
        ),
        schema=PROFILE,
    )

    op.create_table(
        "privacy_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("dm_policy", sa.Text(), server_default=sa.text("'friends'"), nullable=False),
        sa.Column(
            "comment_policy", sa.Text(), server_default=sa.text("'everyone'"), nullable=False
        ),
        sa.Column(
            "mention_policy", sa.Text(), server_default=sa.text("'everyone'"), nullable=False
        ),
        sa.Column(
            "friends_list_visibility",
            sa.Text(),
            server_default=sa.text("'friends'"),
            nullable=False,
        ),
        sa.Column(
            "followers_list_visibility",
            sa.Text(),
            server_default=sa.text("'friends'"),
            nullable=False,
        ),
        sa.Column(
            "presence_visibility", sa.Text(), server_default=sa.text("'friends'"), nullable=False
        ),
        sa.Column(
            "default_post_visibility",
            sa.Text(),
            server_default=sa.text("'friends'"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_privacy_settings"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{IDENTITY}.users.id"],
            name="fk_privacy_settings_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "dm_policy IN ('everyone', 'friends', 'nobody')",
            name=op.f("ck_privacy_settings_dm_policy"),
        ),
        sa.CheckConstraint(
            "comment_policy IN ('everyone', 'friends', 'nobody')",
            name=op.f("ck_privacy_settings_comment_policy"),
        ),
        sa.CheckConstraint(
            "mention_policy IN ('everyone', 'friends', 'nobody')",
            name=op.f("ck_privacy_settings_mention_policy"),
        ),
        sa.CheckConstraint(
            "friends_list_visibility IN ('everyone', 'friends', 'only_me')",
            name=op.f("ck_privacy_settings_friends_list_visibility"),
        ),
        sa.CheckConstraint(
            "followers_list_visibility IN ('everyone', 'friends', 'only_me')",
            name=op.f("ck_privacy_settings_followers_list_visibility"),
        ),
        sa.CheckConstraint(
            "presence_visibility IN ('everyone', 'friends', 'nobody')",
            name=op.f("ck_privacy_settings_presence_visibility"),
        ),
        sa.CheckConstraint(
            "default_post_visibility IN ('public', 'friends', 'private')",
            name=op.f("ck_privacy_settings_default_post_visibility"),
        ),
        schema=PROFILE,
    )

    # Уже зарегистрированные аккаунты (S1, S2) получают профиль по умолчанию: имя совпадает с ником,
    # приватность как у новых аккаунтов (расширить, мигрировать, сузить: код S2 этих таблиц не знает).
    op.execute(
        f"INSERT INTO {PROFILE}.profiles (user_id, display_name) "
        f"SELECT id, username::text FROM {IDENTITY}.users"
    )
    op.execute(f"INSERT INTO {PROFILE}.privacy_settings (user_id) SELECT id FROM {IDENTITY}.users")

    op.create_table(
        "username_reservations",
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("username", name="pk_username_reservations"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{IDENTITY}.users.id"],
            name="fk_username_reservations_user_id_users",
            ondelete="CASCADE",
        ),
        schema=IDENTITY,
    )
    op.create_index(
        "ix_username_reservations_user_id", "username_reservations", ["user_id"], schema=IDENTITY
    )
    op.create_index(
        "ix_username_reservations_reserved_until",
        "username_reservations",
        ["reserved_until"],
        schema=IDENTITY,
    )


def downgrade() -> None:
    op.drop_table("username_reservations", schema=IDENTITY)
    op.drop_table("privacy_settings", schema=PROFILE)
    op.drop_table("profiles", schema=PROFILE)

"""foundation: расширения, схемы, права и таблицы схемы platform

Revision ID: 0001
Revises:
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Миграции неизменяемы, поэтому список схем и расширений записан здесь, а не импортируется из кода.
SCHEMAS = (
    "identity",
    "profile",
    "social",
    "content",
    "chat",
    "notify",
    "media",
    "moderation",
    "platform",
)
EXTENSIONS = ("citext", "pg_trgm", "unaccent")


def _require_roles() -> None:
    """Роли создаёт `messunjerr db-init`; миграция только выдаёт им права."""
    found = set(
        op.get_bind()
        .execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN ('app', 'readonly')"))
        .scalars()
    )
    missing = {"app", "readonly"} - found
    if missing:
        raise RuntimeError(
            f"Не найдены роли {sorted(missing)}: сначала выполните `python -m messunjerr db-init`"
        )


def upgrade() -> None:
    _require_roles()
    # Таблицу версий создаёт сам Alembic от имени migrator. Приложение читает её в проверке
    # готовности (/health/ready), но менять не должно.
    op.execute("GRANT SELECT ON TABLE public.alembic_version TO app")

    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO app, readonly")
        # Права на будущие таблицы: приложение читает и пишет, readonly только читает.
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO readonly"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO app"
        )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "headers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
        sa.UniqueConstraint("event_id", name="uq_outbox_event_id"),
        schema="platform",
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["id"],
        schema="platform",
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "inbox",
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name="pk_inbox"),
        schema="platform",
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_idempotency_keys"),
        schema="platform",
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        schema="platform",
    )
    # Журнал только добавляется: роль приложения не может править и стирать записи (4.14).
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON platform.audit_log FROM app")


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    op.execute("REVOKE SELECT ON TABLE public.alembic_version FROM app")

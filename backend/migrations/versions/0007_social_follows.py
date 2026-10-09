"""social: подписки и запросы на подписку

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOCIAL = "social"
IDENTITY = "identity"


def _user_key(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{IDENTITY}.users.id"],
        name=f"fk_{table}_{column}_users",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    # Права `app` и `readonly` на новые таблицы выдают права по умолчанию из миграции 0001.
    op.create_table(
        "follows",
        sa.Column("follower_id", sa.Uuid(), nullable=False),
        sa.Column("followee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("follower_id", "followee_id", name="pk_follows"),
        _user_key("follower_id", "follows"),
        _user_key("followee_id", "follows"),
        sa.CheckConstraint("follower_id <> followee_id", name=op.f("ck_follows_distinct_users")),
        schema=SOCIAL,
    )
    op.create_index("ix_follows_follower", "follows", ["follower_id", "created_at"], schema=SOCIAL)
    op.create_index("ix_follows_followee", "follows", ["followee_id", "created_at"], schema=SOCIAL)

    op.create_table(
        "follow_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("follower_id", sa.Uuid(), nullable=False),
        sa.Column("followee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_follow_requests"),
        _user_key("follower_id", "follow_requests"),
        _user_key("followee_id", "follow_requests"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'cancelled')",
            name=op.f("ck_follow_requests_status"),
        ),
        sa.CheckConstraint(
            "follower_id <> followee_id", name=op.f("ck_follow_requests_distinct_users")
        ),
        schema=SOCIAL,
    )
    op.create_index(
        "ux_follow_requests_pending",
        "follow_requests",
        ["follower_id", "followee_id"],
        unique=True,
        schema=SOCIAL,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_follow_requests_followee",
        "follow_requests",
        ["followee_id", "created_at"],
        schema=SOCIAL,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("follow_requests", schema=SOCIAL)
    op.drop_table("follows", schema=SOCIAL)

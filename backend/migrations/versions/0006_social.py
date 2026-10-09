"""social: заявки в друзья, дружба и блокировки

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
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
        "friend_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("receiver_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_friend_requests"),
        _user_key("sender_id", "friend_requests"),
        _user_key("receiver_id", "friend_requests"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'cancelled')",
            name=op.f("ck_friend_requests_status"),
        ),
        sa.CheckConstraint(
            "sender_id <> receiver_id", name=op.f("ck_friend_requests_distinct_users")
        ),
        schema=SOCIAL,
    )
    op.create_index(
        "ux_friend_requests_pending",
        "friend_requests",
        [sa.text("LEAST(sender_id, receiver_id)"), sa.text("GREATEST(sender_id, receiver_id)")],
        unique=True,
        schema=SOCIAL,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_friend_requests_receiver",
        "friend_requests",
        ["receiver_id", "created_at"],
        schema=SOCIAL,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_friend_requests_sender",
        "friend_requests",
        ["sender_id", "created_at"],
        schema=SOCIAL,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "friendships",
        sa.Column("user_low_id", sa.Uuid(), nullable=False),
        sa.Column("user_high_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_low_id", "user_high_id", name="pk_friendships"),
        _user_key("user_low_id", "friendships"),
        _user_key("user_high_id", "friendships"),
        sa.CheckConstraint("user_low_id < user_high_id", name=op.f("ck_friendships_ordered_pair")),
        schema=SOCIAL,
    )
    op.create_index("ix_friendships_high", "friendships", ["user_high_id"], schema=SOCIAL)

    op.create_table(
        "blocks",
        sa.Column("blocker_id", sa.Uuid(), nullable=False),
        sa.Column("blocked_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("blocker_id", "blocked_id", name="pk_blocks"),
        _user_key("blocker_id", "blocks"),
        _user_key("blocked_id", "blocks"),
        sa.CheckConstraint("blocker_id <> blocked_id", name=op.f("ck_blocks_distinct_users")),
        schema=SOCIAL,
    )
    op.create_index("ix_blocks_blocked", "blocks", ["blocked_id"], schema=SOCIAL)
    # Взаимной блокировки не бывает: не больше одной строки на пару, в любом направлении.
    op.create_index(
        "ux_blocks_pair",
        "blocks",
        [sa.text("LEAST(blocker_id, blocked_id)"), sa.text("GREATEST(blocker_id, blocked_id)")],
        unique=True,
        schema=SOCIAL,
    )


def downgrade() -> None:
    op.drop_table("blocks", schema=SOCIAL)
    op.drop_table("friendships", schema=SOCIAL)
    op.drop_table("friend_requests", schema=SOCIAL)

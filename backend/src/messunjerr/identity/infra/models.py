"""Таблицы схемы `identity` (4.5): пользователи, сессии, письменные токены.

Отличие от DDL спецификации: вместо `age_declared_at` у `users` столбцы `terms_version` и
`terms_accepted_at` (упрощённый учёт согласия, план спринтов 1.2).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from messunjerr.core.db import Base
from messunjerr.core.ids import uuid7

IDENTITY_SCHEMA = "identity"

ROLES = ("user", "moderator", "admin")
USER_STATUSES = ("pending", "active", "suspended", "banned", "deletion_pending")
EMAIL_TOKEN_PURPOSES = ("verify_email", "reset_password", "change_email")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "username::text = lower(username::text) AND username::text ~ '^[a-z0-9_]{3,30}$'",
            name="username_format",
        ),
        CheckConstraint(_in("role", ROLES), name="role"),
        CheckConstraint(_in("status", USER_STATUSES), name="status"),
        {"schema": IDENTITY_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    username: Mapped[str] = mapped_column(CITEXT, unique=True)
    username_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_version: Mapped[str] = mapped_column(Text)
    terms_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default=text("'user'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionRow(Base):
    """Сессия входа. Refresh-токен хранится как SHA-256 (ротация и обнаружение повтора: S2)."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", postgresql_where=text("revoked_at IS NULL")),
        Index(
            "ix_sessions_prev",
            "prev_refresh_hash",
            postgresql_where=text("prev_refresh_hash IS NOT NULL"),
        ),
        {"schema": IDENTITY_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{IDENTITY_SCHEMA}.users.id", ondelete="CASCADE")
    )
    refresh_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    prev_refresh_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text)


class UsernameReservationRow(Base):
    """Прежний ник после смены (5.3): `reserved_until` другие его взять не могут (S3-03).

    Таблицы нет в DDL 4.5: там сказано «старый ник освобождается через 30 дней», а где это хранить,
    не сказано. Запись живёт столько же, сколько пауза между сменами; просроченные строки удаляет
    плановая очистка, а до неё они просто не учитываются.
    """

    __tablename__ = "username_reservations"
    __table_args__ = (
        Index("ix_username_reservations_user_id", "user_id"),
        Index("ix_username_reservations_reserved_until", "reserved_until"),
        {"schema": IDENTITY_SCHEMA},
    )

    username: Mapped[str] = mapped_column(CITEXT, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{IDENTITY_SCHEMA}.users.id", ondelete="CASCADE")
    )
    reserved_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailTokenRow(Base):
    """Одноразовый токен из письма: подтверждение почты, сброс пароля, смена почты."""

    __tablename__ = "email_tokens"
    __table_args__ = (
        CheckConstraint(_in("purpose", EMAIL_TOKEN_PURPOSES), name="purpose"),
        Index("ix_email_tokens_user_id", "user_id"),
        {"schema": IDENTITY_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{IDENTITY_SCHEMA}.users.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    new_email: Mapped[str | None] = mapped_column(CITEXT)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

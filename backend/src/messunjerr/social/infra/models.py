"""Таблицы схемы `social` (4.5): заявки в друзья, дружба, блокировки, подписки и запросы на подписку.

Отличия от DDL спецификации:

- индексы строятся без `DESC`: PostgreSQL читает их и назад, а порядок столбцов в индексе Alembic
  сверяет ненадёжно (так же сделано в `media.assets`);
- у заявок есть второй частичный индекс `ix_friend_requests_sender` по отправителю: без него список
  исходящих заявок (`GET /friend-requests?direction=outgoing`) читал бы таблицу целиком;
- у блокировок есть уникальный индекс по неупорядоченной паре `ux_blocks_pair`: взаимной блокировки
  не бывает (4.6), и команды, забывшие это проверить, упадут, а не создадут вторую строку;
- у подписок индексы без `DESC` и два, а не один: `ix_follows_follower` обслуживает «на кого я
  подписан», `ix_follows_followee` «кто подписан на меня» (в DDL спецификации только второй).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Uuid,
    func,
    literal_column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from messunjerr.core.db import Base
from messunjerr.core.ids import uuid7
from messunjerr.social.domain.rules import FOLLOW_REQUEST_STATUSES, FRIEND_REQUEST_STATUSES

SOCIAL_SCHEMA = "social"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class FriendRequestRow(Base):
    __tablename__ = "friend_requests"
    __table_args__ = (
        CheckConstraint(_in("status", FRIEND_REQUEST_STATUSES), name="status"),
        CheckConstraint("sender_id <> receiver_id", name="distinct_users"),
        # Не больше одной активной заявки на пару, в любом направлении: вторая вставка падает
        # даже тогда, когда команда забыла взять замок пары.
        Index(
            "ux_friend_requests_pending",
            text("LEAST(sender_id, receiver_id)"),
            text("GREATEST(sender_id, receiver_id)"),
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_friend_requests_receiver",
            "receiver_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_friend_requests_sender",
            "sender_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": SOCIAL_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE")
    )
    receiver_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def is_pending() -> ColumnElement[bool]:
    """`status = 'pending'` литералом, а не параметром запроса.

    Частичные индексы заявок построены по этому условию. Подготовленный запрос с параметром `$n` при
    общем плане (PostgreSQL выбирает его сам, когда он дешевле) индекса не видит и читает таблицу
    целиком; проверено `EXPLAIN` на 300 000 строк. Литерал годится и общему плану.
    """
    return FriendRequestRow.status == literal_column("'pending'")


class FriendshipRow(Base):
    """Дружба хранится один раз: `user_low_id < user_high_id` (порядок как у `ordered_pair`)."""

    __tablename__ = "friendships"
    __table_args__ = (
        CheckConstraint("user_low_id < user_high_id", name="ordered_pair"),
        Index("ix_friendships_high", "user_high_id"),
        {"schema": SOCIAL_SCHEMA},
    )

    user_low_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    user_high_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BlockRow(Base):
    __tablename__ = "blocks"
    __table_args__ = (
        CheckConstraint("blocker_id <> blocked_id", name="distinct_users"),
        Index("ix_blocks_blocked", "blocked_id"),
        # Не больше одной блокировки на пару, в любом направлении: взаимной блокировки не бывает.
        Index(
            "ux_blocks_pair",
            text("LEAST(blocker_id, blocked_id)"),
            text("GREATEST(blocker_id, blocked_id)"),
            unique=True,
        ),
        {"schema": SOCIAL_SCHEMA},
    )

    blocker_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowRow(Base):
    """Подтверждённая подписка: `follower_id` подписан на `followee_id` (4.5)."""

    __tablename__ = "follows"
    __table_args__ = (
        CheckConstraint("follower_id <> followee_id", name="distinct_users"),
        Index("ix_follows_follower", "follower_id", "created_at"),
        Index("ix_follows_followee", "followee_id", "created_at"),
        {"schema": SOCIAL_SCHEMA},
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    followee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowRequestRow(Base):
    """Запрос на подписку на закрытый профиль: ответ владельца превращает его в подписку."""

    __tablename__ = "follow_requests"
    __table_args__ = (
        CheckConstraint(_in("status", FOLLOW_REQUEST_STATUSES), name="status"),
        CheckConstraint("follower_id <> followee_id", name="distinct_users"),
        # Не больше одного ждущего запроса от одного человека к другому.
        Index(
            "ux_follow_requests_pending",
            "follower_id",
            "followee_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_follow_requests_followee",
            "followee_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": SOCIAL_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    follower_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE")
    )
    followee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def is_follow_pending() -> ColumnElement[bool]:
    """`status = 'pending'` литералом (как `is_pending()` у заявок в друзья): частичные индексы
    запросов на подписку видны и общему плану подготовленного запроса."""
    return FollowRequestRow.status == literal_column("'pending'")

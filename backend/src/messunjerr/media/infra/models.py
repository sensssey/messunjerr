"""Таблицы схемы `media` (4.5): ресурсы (файлы), их состояние и ключи объектов в хранилище.

Отличия от DDL спецификации:

- индекс `ix_assets_owner` строится по `created_at` без `DESC`: PostgreSQL читает такой индекс и
  назад, а порядок столбцов в индексе Alembic сверяет ненадёжно;
- столбцы `deleted_at` (когда ресурс удалён) и `objects_deleted_at` (когда его объекты убраны из
  хранилища): без них потерянную постановку `delete_media_objects` нечем найти и осиротевшие объекты
  остались бы в хранилище навсегда (временная схема без Kafka, план спринтов S5-06 и S5-07);
- индексы `ix_assets_reconcile` и `ix_assets_objects_pending` обслуживают плановую сверку.

Внешний ключ `profile.profiles.avatar_asset_id` на эту таблицу создаёт миграция 0004.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from messunjerr.core.db import Base
from messunjerr.core.ids import uuid7
from messunjerr.media.domain.rules import KINDS, PURPOSES, STATUSES

MEDIA_SCHEMA = "media"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class AssetRow(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="kind"),
        CheckConstraint(_in("purpose", PURPOSES), name="purpose"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint("declared_size > 0", name="declared_size_positive"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_not_negative"),
        Index("ix_assets_owner", "owner_id", "created_at"),
        Index(
            "ix_assets_cleanup",
            "created_at",
            postgresql_where=text("status IN ('pending','uploaded')"),
        ),
        Index(
            "ix_assets_reconcile",
            "uploaded_at",
            postgresql_where=text("status IN ('uploaded','processing')"),
        ),
        Index(
            "ix_assets_objects_pending",
            "created_at",
            postgresql_where=text(
                "status IN ('deleted','rejected') AND objects_deleted_at IS NULL"
            ),
        ),
        {"schema": MEDIA_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identity.users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    original_filename: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    variants: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    objects_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

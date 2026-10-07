"""Модели ответов media (5.8): карточка ресурса и квота."""

import uuid

from pydantic import BaseModel, ConfigDict

from messunjerr.core.schemas import UtcDateTime
from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status


class AssetUrls(BaseModel):
    """Ссылки на файл и его варианты. Пока ресурс не `ready`, все равны `null`; ссылки выдаёт S6."""

    model_config = ConfigDict(frozen=True)

    thumb: str | None = None
    medium: str | None = None
    original: str | None = None


class Asset(BaseModel):
    """Карточка ресурса для владельца."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
                    "purpose": "post",
                    "kind": "image",
                    "status": "pending",
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "declared_size": 1843200,
                    "size_bytes": None,
                    "width": None,
                    "height": None,
                    "reject_reason": None,
                    "urls": {"thumb": None, "medium": None, "original": None},
                    "url_expires_at": None,
                    "created_at": "2026-10-07T12:34:56.789Z",
                    "uploaded_at": None,
                    "processed_at": None,
                },
                {
                    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d81",
                    "purpose": "avatar",
                    "kind": "image",
                    "status": "rejected",
                    "filename": "face.png",
                    "content_type": "image/png",
                    "declared_size": 90210,
                    "size_bytes": 90210,
                    "width": None,
                    "height": None,
                    "reject_reason": "not_an_image",
                    "urls": {"thumb": None, "medium": None, "original": None},
                    "url_expires_at": None,
                    "created_at": "2026-10-07T12:34:56.789Z",
                    "uploaded_at": "2026-10-07T12:35:10.120Z",
                    "processed_at": "2026-10-07T12:35:11.004Z",
                },
            ]
        },
    )

    id: uuid.UUID
    purpose: Purpose
    kind: Kind
    status: Status
    filename: str | None
    content_type: str | None
    declared_size: int
    size_bytes: int | None
    width: int | None
    height: int | None
    reject_reason: RejectReason | None
    urls: AssetUrls
    url_expires_at: UtcDateTime | None
    created_at: UtcDateTime
    uploaded_at: UtcDateTime | None
    processed_at: UtcDateTime | None


class Quota(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [{"used_bytes": 183456789, "limit_bytes": 1073741824, "assets_count": 42}]
        },
    )

    used_bytes: int
    limit_bytes: int
    assets_count: int

"""Модели ответов media (5.8, 5.1): карточка ресурса, вложение `MediaRef`, ссылки на файлы и квота."""

import uuid

from pydantic import BaseModel, ConfigDict

from messunjerr.core.schemas import UtcDateTime
from messunjerr.media.domain.rules import Kind, Purpose, RejectReason, Status


class AssetUrls(BaseModel):
    """Ссылки на файл и его варианты; пока ресурс не `ready`, все равны `null`.

    У изображения `thumb` и `medium` (аватар 64 и 256 пикселей, фото 320 и 1280); `original` только у
    GIF. У файла только `original`. Ссылки на аватар публичны и постоянны, остальные это presigned GET
    на десять минут (`url_expires_at`).
    """

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


class MediaRef(BaseModel):
    """Вложение поста или сообщения (5.1): компактная карточка ресурса без служебных полей.

    Контексты выше (посты S11, сообщения S14) собирают её через `AssetPresenter.ref`, чтобы каждый
    ответ с вложениями нёс свежие ссылки; сам ресурс по-прежнему принадлежит media.
    """

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80",
                    "kind": "image",
                    "status": "ready",
                    "content_type": "image/webp",
                    "size_bytes": 184233,
                    "filename": "photo.jpg",
                    "width": 1280,
                    "height": 960,
                    "urls": {
                        "thumb": "https://messunjerr.localhost/media/uploads/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/thumb.webp?X-Amz-Signature=0f3a",
                        "medium": "https://messunjerr.localhost/media/uploads/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/medium.webp?X-Amz-Signature=91b2",
                        "original": None,
                    },
                    "url_expires_at": "2026-10-04T12:44:56.000Z",
                }
            ]
        },
    )

    id: uuid.UUID
    kind: Kind
    status: Status
    content_type: str | None
    size_bytes: int | None
    filename: str | None
    width: int | None
    height: int | None
    urls: AssetUrls
    url_expires_at: UtcDateTime | None


class AssetLinks(BaseModel):
    """Свежие ссылки на файлы ресурса (`GET /media/{asset_id}/urls`)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "urls": {
                        "thumb": "https://messunjerr.localhost/media/uploads/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/thumb.webp?X-Amz-Signature=0f3a",
                        "medium": "https://messunjerr.localhost/media/uploads/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/medium.webp?X-Amz-Signature=91b2",
                        "original": None,
                    },
                    "url_expires_at": "2026-10-07T12:45:56.789Z",
                }
            ]
        },
    )

    urls: AssetUrls
    url_expires_at: UtcDateTime | None


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

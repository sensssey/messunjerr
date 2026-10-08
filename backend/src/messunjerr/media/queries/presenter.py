"""Карточка ресурса со ссылками на файлы (S6-03, 5.1 `MediaRef`, 5.8 `Asset`).

Ссылки появляются только у готового ресурса:

- **аватар** отдаётся публично и навсегда: `{публичный адрес}/{bucket}/public/avatars/{id}/64.webp` и
  `…/256.webp` (в `thumb` и `medium`), срока у них нет, `url_expires_at` пуст;
- **фото поста и сообщения**: presigned GET на `thumb` и `medium` на десять минут; у GIF ещё `original`
  (анимацию мы не обрабатываем, оригинал хранится как есть); у остальных изображений оригинала нет;
- **файл**: presigned GET на `original` с `Content-Disposition: attachment` и именем файла.

Заголовки ответа хранилища задаёт сама ссылка (`response-content-*`), а не метаданные объекта: им
верить нельзя, файл мог объявить себя чем угодно (4.11). Подпись считается локально, в сеть презентер
не ходит.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from messunjerr.core.clock import utcnow
from messunjerr.media.domain.ports import ObjectStorage
from messunjerr.media.domain.rules import (
    AVATAR_PURPOSES,
    DEFAULT_FILENAME,
    OCTET_STREAM,
    ORIGINAL,
    PRIVATE_CACHE_CONTROL,
    WEBP,
    Kind,
    Purpose,
    Status,
    attachment_disposition,
)
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.queries.assets import asset_dto
from messunjerr.media.queries.models import Asset, AssetUrls, MediaRef
from messunjerr.settings import Settings

GIF = "image/gif"


@dataclass(frozen=True, slots=True)
class Links:
    urls: AssetUrls
    expires_at: datetime | None
    """Когда перестанут действовать ссылки; `None`, если все публичные либо ссылок нет."""


NO_LINKS = Links(AssetUrls(), None)


def _key(variants: dict[str, Any], name: str) -> str | None:
    variant = variants.get(name)
    key = variant.get("key") if isinstance(variant, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return key if isinstance(key, str) else None


class AssetPresenter:
    def __init__(self, storage: ObjectStorage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    def _public_url(self, key: str | None) -> str | None:
        if key is None:
            return None
        return f"{self._settings.storage_public_url}/{self._settings.s3_bucket}/{key}"

    async def _signed(self, key: str | None, **headers: str) -> str | None:
        if key is None:
            return None
        return await self._storage.presign_get(
            key=key, expires_in=self._settings.download_url_ttl_seconds, **headers
        )

    async def links(self, row: AssetRow, *, now: datetime | None = None) -> Links:
        """Ссылки на файлы ресурса; пока он не `ready` (и у ресурсов S5 без вариантов) их нет."""
        if row.status != Status.READY:
            return NO_LINKS
        moment = now or utcnow()
        expires_at = moment + timedelta(seconds=self._settings.download_url_ttl_seconds)
        variants: dict[str, Any] = row.variants

        if Kind(row.kind) is Kind.FILE:
            original = await self._signed(
                row.object_key,
                content_type=OCTET_STREAM,
                content_disposition=attachment_disposition(
                    row.original_filename or DEFAULT_FILENAME
                ),
                cache_control=PRIVATE_CACHE_CONTROL,
            )
            return Links(AssetUrls(original=original), expires_at)

        if not variants:
            return NO_LINKS  # готовое изображение времён S5: его пересоберёт обработка
        if Purpose(row.purpose) in AVATAR_PURPOSES:
            return Links(
                AssetUrls(
                    thumb=self._public_url(_key(variants, "thumb")),
                    medium=self._public_url(_key(variants, "medium")),
                ),
                None,
            )
        urls = AssetUrls(
            thumb=await self._signed(
                _key(variants, "thumb"), content_type=WEBP, cache_control=PRIVATE_CACHE_CONTROL
            ),
            medium=await self._signed(
                _key(variants, "medium"), content_type=WEBP, cache_control=PRIVATE_CACHE_CONTROL
            ),
            original=await self._signed(
                _key(variants, ORIGINAL), content_type=GIF, cache_control=PRIVATE_CACHE_CONTROL
            ),
        )
        return Links(urls, expires_at)

    async def card(self, row: AssetRow, *, now: datetime | None = None) -> Asset:
        """Карточка ресурса владельца со ссылками."""
        links = await self.links(row, now=now)
        return asset_dto(row, links.urls, links.expires_at)

    async def ref(self, row: AssetRow, *, now: datetime | None = None) -> MediaRef:
        """Вложение для поста или сообщения (5.1): то, что видит любой, кому показан объект."""
        links = await self.links(row, now=now)
        return MediaRef(
            id=row.id,
            kind=Kind(row.kind),
            status=Status(row.status),
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            filename=row.original_filename,
            width=row.width,
            height=row.height,
            urls=links.urls,
            url_expires_at=links.expires_at,
        )

"""Схемы запросов и ответов media (5.8).

В теле `POST /media/uploads` нет перечислений и границ размера на уровне схемы: назначение, тип,
расширение и размер проверяет команда и отвечает кодами из каталога (`purpose_invalid`,
`content_type_not_allowed`, `extension_forbidden`, `size_invalid`, `size_exceeds_limit`), которых у
общих ошибок pydantic нет.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pydantic.config import JsonDict

from messunjerr.core.schemas import RAW, ApiModel, UtcDateTime
from messunjerr.media.domain.rules import FILENAME_MAX_LENGTH
from messunjerr.media.queries.models import Asset

_PENDING_ASSET: JsonDict = {
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
}


class InitUploadRequest(ApiModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "purpose": "post",
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": 1843200,
                }
            ]
        }
    )

    # Пустые `purpose` и `content_type` не отвергает схема: команда ответит кодами каталога
    # `purpose_invalid` и `content_type_not_allowed`, как и на любое другое неверное значение.
    purpose: Annotated[str, Field(max_length=32, examples=["post"])]
    filename: Annotated[
        str,
        RAW,  # имя очищает команда (пути, управляющие символы), а не отвергает схема
        Field(min_length=1, max_length=FILENAME_MAX_LENGTH, examples=["photo.jpg"]),
    ]
    content_type: Annotated[str, Field(max_length=255, examples=["image/jpeg"])]
    size_bytes: Annotated[StrictInt, Field(examples=[1843200])]


class UploadInstructions(BaseModel):
    """Что сделать клиенту: `PUT` файла на `url` с этими заголовками до `expires_at`.

    Запись условная (`If-None-Match: *`): объект создаётся один раз, повторный `PUT` по этой ссылке
    получает `412`, и тогда файл уже на месте: остаётся вызвать `complete`.
    """

    model_config = ConfigDict(frozen=True)

    method: Literal["PUT"]
    url: str
    headers: dict[str, str]
    expires_at: UtcDateTime


class InitUploadResponse(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "asset": _PENDING_ASSET,
                    "upload": {
                        "method": "PUT",
                        "url": "https://messunjerr.localhost/media/uploads/0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80/original?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=0f3a",
                        "headers": {"Content-Type": "image/jpeg", "If-None-Match": "*"},
                        "expires_at": "2026-10-07T12:49:56.789Z",
                    },
                }
            ]
        },
    )

    asset: Asset
    upload: UploadInstructions


class CompleteUploadResponse(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "asset": {
                        **_PENDING_ASSET,
                        "status": "uploaded",
                        "size_bytes": 1843200,
                        "uploaded_at": "2026-10-07T12:35:10.120Z",
                    }
                }
            ]
        },
    )

    asset: Asset

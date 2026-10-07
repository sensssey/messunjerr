"""Команда `POST /media/uploads` (5.8, S5-04): проверить заявку, занять квоту и выдать presigned PUT.

Все найденные ошибки полей приходят одним ответом `422`. После них идёт квота (`403`): место
считается с резервом заявленного размера идущих загрузок, а проверка и вставка выполняются под
блокировкой владельца, чтобы две параллельные заявки не прошли в одну и ту же свободную дыру.
Клиент загружает файл `PUT`-ом прямо в хранилище; сервер содержимого не видит.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import ErrorItem, ServiceUnavailableError, ValidationFailedError
from messunjerr.core.ids import uuid7
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.domain import errors
from messunjerr.media.domain.ports import ObjectStorage, StorageUnavailableError
from messunjerr.media.domain.rules import (
    AVATAR_CONTENT_TYPES,
    AVATAR_PURPOSES,
    PURPOSES,
    Kind,
    Purpose,
    Status,
    extension_of,
    is_forbidden_extension,
    kind_for,
    max_bytes,
    normalize_content_type,
    object_key,
    sanitize_filename,
    signed_content_type,
)
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.infra.repositories import AssetRepository
from messunjerr.media.queries.assets import asset_dto
from messunjerr.media.queries.models import Asset
from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class InitUpload:
    owner_id: uuid.UUID
    purpose: str
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadInstructions:
    """Что должен сделать клиент: `PUT` на `url` с этими заголовками до `expires_at`."""

    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InitUploadResult:
    asset: Asset
    upload: UploadInstructions


@dataclass(frozen=True, slots=True)
class _Checked:
    purpose: Purpose
    kind: Kind
    content_type: str
    filename: str


def _check(command: InitUpload) -> _Checked:
    """Проверка полей заявки; при любой ошибке `422` со всем списком найденного."""
    problems: list[ErrorItem] = []
    filename = sanitize_filename(command.filename)

    purpose: Purpose | None = None
    if command.purpose in PURPOSES:
        purpose = Purpose(command.purpose)
    else:
        problems.append(errors.purpose_invalid(PURPOSES))

    content_type = normalize_content_type(command.content_type)
    kind = Kind.FILE
    if content_type is None:
        problems.append(errors.content_type_not_allowed())
    else:
        kind = kind_for(content_type)
        if purpose in AVATAR_PURPOSES and content_type not in AVATAR_CONTENT_TYPES:
            problems.append(errors.content_type_not_allowed(tuple(sorted(AVATAR_CONTENT_TYPES))))

    if is_forbidden_extension(filename):
        problems.append(errors.extension_forbidden(extension_of(filename)))

    if command.size_bytes <= 0:
        problems.append(errors.size_invalid())
    elif purpose is not None and command.size_bytes > max_bytes(purpose, kind):
        problems.append(errors.size_exceeds_limit(max_bytes(purpose, kind)))

    if problems or purpose is None or content_type is None:
        raise ValidationFailedError(problems)
    return _Checked(purpose=purpose, kind=kind, content_type=content_type, filename=filename)


async def init_upload(
    command: InitUpload,
    *,
    uow: UnitOfWork,
    settings: Settings,
    storage: ObjectStorage,
    now: datetime | None = None,
) -> InitUploadResult:
    moment = now or utcnow()
    checked = _check(command)

    repository = AssetRepository(uow.session)
    await repository.lock_quota(command.owner_id)
    usage = await repository.usage(command.owner_id)
    if usage.used_bytes + command.size_bytes > settings.media_quota_bytes:
        raise errors.quota_exceeded(limit=settings.media_quota_bytes, used=usage.used_bytes)

    asset_id = uuid7()
    row = AssetRow(
        id=asset_id,
        owner_id=command.owner_id,
        kind=checked.kind,
        purpose=checked.purpose,
        status=Status.PENDING,
        object_key=object_key(asset_id),
        original_filename=checked.filename,
        content_type=checked.content_type,
        declared_size=command.size_bytes,
        created_at=moment,
    )
    repository.add(row)

    try:
        presigned = await storage.presign_put(
            key=row.object_key,
            content_type=signed_content_type(checked.kind, checked.content_type),
            content_length=command.size_bytes,
            expires_in=settings.upload_url_ttl_seconds,
        )
    except StorageUnavailableError as error:
        raise ServiceUnavailableError("The file storage is temporarily unavailable.", 5) from error
    await uow.session.flush()
    await uow.commit()

    get_logger("messunjerr.media").info(
        "upload_initiated",
        asset_id=str(asset_id),
        purpose=checked.purpose.value,
        kind=checked.kind.value,
        size_bytes=command.size_bytes,
    )
    expires_at = moment + timedelta(seconds=settings.upload_url_ttl_seconds)
    return InitUploadResult(
        asset=asset_dto(row),
        upload=UploadInstructions(
            method="PUT", url=presigned.url, headers=presigned.headers, expires_at=expires_at
        ),
    )

"""Маршруты media: `/media/uploads`, `/media/{asset_id}`, `/media/quota` (5.8).

Файлы грузятся напрямую в хранилище по presigned URL: через API они не проходят (лимит тела 1 МБ).
Жизненный цикл ресурса и причины отказов описаны в спецификации 4.11 и 5.8.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep, UowDep
from messunjerr.core.errors import NotFoundError
from messunjerr.core.idempotency import idempotent_route_class
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses
from messunjerr.identity.api_public import PrincipalDep, limit_user, no_store, principal_user_id
from messunjerr.media.api.deps import MediaDep
from messunjerr.media.api.schemas import (
    CompleteUploadResponse,
    InitUploadRequest,
    InitUploadResponse,
    UploadInstructions,
)
from messunjerr.media.commands.complete_upload import CompleteUpload, complete_upload
from messunjerr.media.commands.delete_asset import DeleteAsset, delete_asset
from messunjerr.media.commands.init_upload import InitUpload, init_upload
from messunjerr.media.queries.assets import get_asset, get_quota
from messunjerr.media.queries.models import Asset, Quota

_AUTH_ERRORS = {**TOKEN_ERRORS, **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING)}
"""Ошибки токена и ограничение аккаунта, который ждёт удаления (5.1)."""

_ASSET_ID = Annotated[uuid.UUID, Path(description="Идентификатор ресурса.")]

# `Idempotency-Key` принимает только создание заявки: повтор `complete` и так возвращает состояние.
uploads_router = APIRouter(
    prefix="/media",
    tags=["media"],
    route_class=idempotent_route_class(principal_user_id),
    dependencies=[Depends(no_store)],
)
media_router = APIRouter(prefix="/media", tags=["media"], dependencies=[Depends(no_store)])


@uploads_router.post(
    "/uploads",
    status_code=201,
    response_model=InitUploadResponse,
    summary="Начать загрузку файла",
    description=(
        "Создаёт ресурс `pending` и выдаёт presigned URL. Клиент делает `PUT` файла на `upload.url` "
        "с заголовками из `upload.headers` до `upload.expires_at` (по умолчанию 15 минут), затем зовёт "
        "`POST /media/uploads/{asset_id}/complete`. Подпись закрепляет тип, точный размер файла и "
        "запись один раз (`If-None-Match: *`): повторный `PUT` по той же ссылке получает `412`, "
        "значит файл уже на месте. Принимает `Idempotency-Key`. Лимит `upload_init`."
    ),
    dependencies=[Depends(limit_user("upload_init", "api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.QUOTA_EXCEEDED),
        **problem_responses(ErrorCode.IDEMPOTENCY_KEY_REUSE, ErrorCode.REQUEST_IN_PROGRESS),
        **LIMIT_ERRORS,
    },
)
async def init_upload_endpoint(
    body: InitUploadRequest,
    response: Response,
    principal: PrincipalDep,
    uow: UowDep,
    resources: ResourcesDep,
    media: MediaDep,
) -> InitUploadResponse:
    result = await init_upload(
        InitUpload(
            owner_id=principal.user_id,
            purpose=body.purpose,
            filename=body.filename,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
        ),
        uow=uow,
        settings=resources.settings,
        storage=media.storage,
    )
    response.headers["Location"] = f"/api/v1/media/{result.asset.id}"
    return InitUploadResponse(
        asset=result.asset,
        upload=UploadInstructions(
            method="PUT",
            url=result.upload.url,
            headers=result.upload.headers,
            expires_at=result.upload.expires_at,
        ),
    )


@media_router.post(
    "/uploads/{asset_id}/complete",
    status_code=202,
    response_model=CompleteUploadResponse,
    summary="Завершить загрузку",
    description=(
        "Сервер проверяет объект в хранилище (на месте ли, совпадает ли размер) и передаёт файл на "
        "обработку. Повторный вызов возвращает текущее состояние. Результат обработки (`ready` или "
        "`rejected`) читается через `GET /media/{asset_id}`; SSE-события `media.ready` и "
        "`media.rejected` появятся вместе с потоком событий (S10). Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(
            ErrorCode.NOT_FOUND, ErrorCode.UPLOAD_MISSING, ErrorCode.UPLOAD_REJECTED
        ),
        **LIMIT_ERRORS,
    },
)
async def complete_upload_endpoint(
    asset_id: _ASSET_ID,
    principal: PrincipalDep,
    uow: UowDep,
    resources: ResourcesDep,
    media: MediaDep,
) -> CompleteUploadResponse:
    asset = await complete_upload(
        CompleteUpload(owner_id=principal.user_id, asset_id=asset_id),
        uow=uow,
        storage=media.storage,
        jobs=resources.jobs,
    )
    return CompleteUploadResponse(asset=asset)


# Маршрут `/quota` объявлен раньше `/{asset_id}`: иначе слово «quota» разбиралось бы как идентификатор.
@media_router.get(
    "/quota",
    response_model=Quota,
    summary="Квота хранилища",
    description=(
        "Занятое место: размер готовых файлов плюс заявленный размер идущих загрузок. Лимит "
        "`api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={**_AUTH_ERRORS, **LIMIT_ERRORS},
)
async def quota_endpoint(principal: PrincipalDep, uow: UowDep, resources: ResourcesDep) -> Quota:
    return await get_quota(uow.session, owner_id=principal.user_id, settings=resources.settings)


@media_router.get(
    "/{asset_id}",
    response_model=Asset,
    summary="Карточка ресурса",
    description=(
        "Состояние своего ресурса: для опроса, если SSE недоступен. Пока ресурс не `ready`, "
        "`urls` равны `null`. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={**_AUTH_ERRORS, **problem_responses(ErrorCode.NOT_FOUND), **LIMIT_ERRORS},
)
async def read_asset_endpoint(asset_id: _ASSET_ID, principal: PrincipalDep, uow: UowDep) -> Asset:
    asset = await get_asset(uow.session, asset_id=asset_id, owner_id=principal.user_id)
    if asset is None:
        raise NotFoundError("The asset does not exist or is not yours.")
    return asset


@media_router.delete(
    "/{asset_id}",
    status_code=204,
    summary="Удалить ресурс",
    description=(
        "Допустимо, пока ресурс ни к чему не привязан (аватар, пост, сообщение). Объекты хранилища "
        "удаляет фоновая задача. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.ASSET_IN_USE),
        **LIMIT_ERRORS,
    },
)
async def delete_asset_endpoint(
    asset_id: _ASSET_ID,
    principal: PrincipalDep,
    uow: UowDep,
    resources: ResourcesDep,
    media: MediaDep,
) -> Response:
    await delete_asset(
        DeleteAsset(owner_id=principal.user_id, asset_id=asset_id),
        uow=uow,
        usage=media.usage,
        jobs=resources.jobs,
    )
    return Response(status_code=204)


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(uploads_router)
api_router.include_router(media_router)

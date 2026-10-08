"""Маршруты profiles: `/me/profile`, `/me/privacy`, `/users/{ref}` (5.3).

Роутеры тонкие: разбирают вход, вызывают команду или запрос и формируют ответ (4.3).
`PATCH /me/username`, `DELETE /me` и `POST /me/restore` принадлежат identity: ник и статус аккаунта
лежат в его таблицах.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep, UowDep
from messunjerr.core.errors import NotFoundError
from messunjerr.core.me import MeProfile, PrivacySettings
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses
from messunjerr.identity.api_public import PrincipalDep, limit_user, no_store
from messunjerr.profiles.api.deps import ProfilesDep
from messunjerr.profiles.api.schemas import UpdatePrivacyRequest, UpdateProfileRequest
from messunjerr.profiles.commands.update_privacy import UpdatePrivacy, update_privacy
from messunjerr.profiles.commands.update_profile import UpdateProfile, update_profile
from messunjerr.profiles.queries.me import get_privacy
from messunjerr.profiles.queries.models import UserProfile
from messunjerr.profiles.queries.user_profile import get_user_profile

profile_router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(no_store)])
users_router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(no_store)])

_AUTH_ERRORS = {**TOKEN_ERRORS, **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING)}
"""Ошибки токена и ограничение аккаунта, который ждёт удаления (5.1)."""


@profile_router.patch(
    "/profile",
    response_model=MeProfile,
    summary="Изменить профиль",
    description=(
        "Как JSON Merge Patch: отсутствующий ключ без изменений, `null` очищает поле (если можно). "
        "`avatar_asset_id`: свой готовый ресурс с назначением `avatar` (`POST /media/uploads`), `null` убирает "
        "аватар; прежний аватар при замене и очистке удаляется вместе с файлами. Чужой, удалённый и "
        "несуществующий ресурс это `asset_not_found`, неготовый `asset_not_ready`, не того назначения "
        "`asset_wrong_purpose`. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **problem_responses(ErrorCode.VALIDATION_ERROR), **LIMIT_ERRORS},
)
async def update_profile_endpoint(
    body: UpdateProfileRequest,
    principal: PrincipalDep,
    uow: UowDep,
    resources: ResourcesDep,
    profiles: ProfilesDep,
) -> MeProfile:
    return await update_profile(
        UpdateProfile(user_id=principal.user_id, changes=body.to_changes()),
        uow=uow,
        settings=resources.settings,
        avatars=profiles.avatars,
    )


@profile_router.get(
    "/privacy",
    response_model=PrivacySettings,
    summary="Настройки приватности",
    description="Кто может писать, комментировать, упоминать и видеть списки. Лимит `api_read`.",
    dependencies=[Depends(limit_user("api_read"))],
    responses={**_AUTH_ERRORS, **LIMIT_ERRORS},
)
async def read_privacy_endpoint(principal: PrincipalDep, uow: UowDep) -> PrivacySettings:
    return await get_privacy(uow.session, principal.user_id)


@profile_router.patch(
    "/privacy",
    response_model=PrivacySettings,
    summary="Изменить настройки приватности",
    description="Любые из полей, значения из перечислений. Ответ: настройки целиком. Лимит `api_write`.",
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **problem_responses(ErrorCode.VALIDATION_ERROR), **LIMIT_ERRORS},
)
async def update_privacy_endpoint(
    body: UpdatePrivacyRequest, principal: PrincipalDep, uow: UowDep
) -> PrivacySettings:
    return await update_privacy(
        UpdatePrivacy(user_id=principal.user_id, changes=body.to_changes()), uow=uow
    )


@users_router.get(
    "/{ref}",
    response_model=UserProfile,
    summary="Профиль человека",
    description=(
        "`ref`: UUID или ник. Что видно, зависит от зрителя: закрытый профиль показывает чужим только "
        "имя, ник, аватар, био и счётчики, дата рождения и счётчики друзей и подписчиков подчиняются "
        "настройкам владельца. Нет такого, заблокирован в любую сторону или аккаунт не `active`: "
        "`404`. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={**_AUTH_ERRORS, **problem_responses(ErrorCode.NOT_FOUND), **LIMIT_ERRORS},
)
async def read_user_endpoint(
    ref: Annotated[str, Path(description="UUID или ник.")],
    principal: PrincipalDep,
    uow: UowDep,
    profiles: ProfilesDep,
) -> UserProfile:
    profile = await get_user_profile(
        uow.session,
        viewer_id=principal.user_id,
        ref=ref,
        relationships=profiles.relationships,
        counters=profiles.counters,
    )
    if profile is None:
        raise NotFoundError("The user does not exist or is not available to you.")
    return profile


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(profile_router)
api_router.include_router(users_router)

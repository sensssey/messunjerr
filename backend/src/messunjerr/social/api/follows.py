"""Маршруты подписок: подписка и отписка, свои списки, запросы на подписку, чужие списки (5.4, 5.3).

Права и порядок проверок живут в политиках (`social.domain.policies`, 4.6): ручки только вызывают
команды и запросы. `Idempotency-Key` ручкам подписок не нужен: каждая идемпотентна сама по себе.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import UowDep
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses
from messunjerr.core.pagination import DEFAULT_LIMIT, CursorParam, Limit, Page
from messunjerr.identity.api_public import PrincipalDep, limit_user, no_store
from messunjerr.profiles.api_public import UserSummary
from messunjerr.social.commands.follows import (
    FollowUser,
    RemoveFollower,
    RespondToFollowRequest,
    UnfollowUser,
    approve_follow_request,
    decline_follow_request,
    follow_user,
    remove_follower,
    unfollow_user,
)
from messunjerr.social.queries.follow_models import ApprovedFollower, FollowRequest, FollowStatus
from messunjerr.social.queries.follows import (
    list_follow_requests,
    list_my_follows,
    list_user_follows,
)
from messunjerr.social.queries.models import UserListItem

_AUTH_ERRORS = {**TOKEN_ERRORS, **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING)}
"""Ошибки токена и ограничение аккаунта, который ждёт удаления (5.1)."""

_USER_ID = Annotated[uuid.UUID, Path(description="Идентификатор человека.")]
_REQUEST_ID = Annotated[uuid.UUID, Path(description="Идентификатор запроса на подписку.")]
_REF = Annotated[str, Path(description="UUID или ник.")]

follows_router = APIRouter(prefix="/follows", tags=["social"], dependencies=[Depends(no_store)])
me_router = APIRouter(prefix="/me", tags=["social"], dependencies=[Depends(no_store)])
users_router = APIRouter(prefix="/users", tags=["social"], dependencies=[Depends(no_store)])

router = APIRouter()
"""Все маршруты подписок одним роутером: `social.api.routers` подключает его одной строкой."""


# ----------------------------------------------------------------------------- подписка и отписка
@follows_router.put(
    "/{user_id}",
    response_model=FollowStatus,
    summary="Подписаться",
    description=(
        "Идемпотентно. Открытый профиль: подписка сразу (`status: following`, событие "
        "`FollowCreated`). Закрытый профиль: создаётся запрос, ответ владельца превращает его в "
        "подписку (`status: requested`, событие `FollowRequested`); дружба на это не влияет. Повтор по "
        "готовой подписке или ждущему запросу отвечает так же и событий не пишет. Сам на себя: `400 "
        "self_action`. Нет такого человека, аккаунт не `active` или блокировка в любую сторону: `404`. "
        "Лимиты `follow` (100 в час) и `api_write`."
    ),
    dependencies=[Depends(limit_user("follow", "api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.SELF_ACTION, ErrorCode.NOT_FOUND),
        **LIMIT_ERRORS,
    },
)
async def follow_user_endpoint(
    user_id: _USER_ID, principal: PrincipalDep, uow: UowDep
) -> FollowStatus:
    return await follow_user(FollowUser(actor_id=principal.user_id, target_id=user_id), uow=uow)


@follows_router.delete(
    "/{user_id}",
    status_code=204,
    response_class=Response,
    summary="Отписаться или отменить запрос",
    description=(
        "Снимает подписку или отменяет свой ждущий запрос на подписку. Всегда `204`, даже если "
        "подписки не было или человека нет: так ничего лишнего не раскрывается. Событие "
        "`FollowRemoved` пишется, только если подписка была. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **LIMIT_ERRORS},
)
async def unfollow_user_endpoint(user_id: _USER_ID, principal: PrincipalDep, uow: UowDep) -> None:
    await unfollow_user(UnfollowUser(actor_id=principal.user_id, target_id=user_id), uow=uow)


# ----------------------------------------------------------------------------- свои списки
@me_router.get(
    "/following",
    response_model=Page[UserSummary],
    summary="Мои подписки",
    description=(
        "На кого вы подписаны, новые сверху. Аккаунты не `active` и люди, связанные с вами "
        "блокировкой, не показываются. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def my_following_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserSummary]:
    return await list_my_follows(
        uow.session, user_id=principal.user_id, followers=False, limit=limit, cursor=cursor
    )


@me_router.get(
    "/followers",
    response_model=Page[UserSummary],
    summary="Мои подписчики",
    description=(
        "Кто подписан на вас, новые сверху. Аккаунты не `active` и люди, связанные с вами "
        "блокировкой, не показываются. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def my_followers_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserSummary]:
    return await list_my_follows(
        uow.session, user_id=principal.user_id, followers=True, limit=limit, cursor=cursor
    )


@me_router.delete(
    "/followers/{user_id}",
    status_code=204,
    response_class=Response,
    summary="Удалить подписчика",
    description=(
        "Подписчик перестаёт быть подписчиком (на закрытый профиль он сможет подписаться заново "
        "только запросом). Всегда `204`, даже если такого подписчика нет. Событие `FollowRemoved` "
        "пишется, только если подписка была. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **LIMIT_ERRORS},
)
async def remove_follower_endpoint(user_id: _USER_ID, principal: PrincipalDep, uow: UowDep) -> None:
    await remove_follower(RemoveFollower(actor_id=principal.user_id, follower_id=user_id), uow=uow)


# ----------------------------------------------------------------------------- запросы на подписку
@me_router.get(
    "/follow-requests",
    response_model=Page[FollowRequest],
    summary="Входящие запросы на подписку",
    description=(
        "Ждущие запросы на подписку на ваш закрытый профиль, новые сверху; `user` это тот, кто "
        "просит. Запросы от аккаунтов не `active` не показываются. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def list_follow_requests_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[FollowRequest]:
    return await list_follow_requests(
        uow.session, user_id=principal.user_id, limit=limit, cursor=cursor
    )


@me_router.post(
    "/follow-requests/{request_id}/approve",
    response_model=ApprovedFollower,
    summary="Одобрить запрос на подписку",
    description=(
        "Только владелец профиля. `200`: новый подписчик; подписку создаёт сам ответ. Чужой запрос, "
        "несуществующий или от аккаунта не `active`: `404`. Запрос уже получил ответ: `409 "
        "follow_request_not_pending`. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.FOLLOW_REQUEST_NOT_PENDING),
        **LIMIT_ERRORS,
    },
)
async def approve_follow_request_endpoint(
    request_id: _REQUEST_ID, principal: PrincipalDep, uow: UowDep
) -> ApprovedFollower:
    return await approve_follow_request(
        RespondToFollowRequest(actor_id=principal.user_id, request_id=request_id), uow=uow
    )


@me_router.post(
    "/follow-requests/{request_id}/decline",
    status_code=204,
    response_class=Response,
    summary="Отклонить запрос на подписку",
    description=(
        "Только владелец профиля; просивший об отказе не уведомляется и может запросить снова. "
        "Ошибки как у одобрения. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.FOLLOW_REQUEST_NOT_PENDING),
        **LIMIT_ERRORS,
    },
)
async def decline_follow_request_endpoint(
    request_id: _REQUEST_ID, principal: PrincipalDep, uow: UowDep
) -> None:
    await decline_follow_request(
        RespondToFollowRequest(actor_id=principal.user_id, request_id=request_id), uow=uow
    )


# ----------------------------------------------------------------------------- списки чужого профиля
_USER_LIST_ERRORS = {
    **_AUTH_ERRORS,
    **problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.LIST_HIDDEN,
        ErrorCode.PROFILE_PRIVATE,
        ErrorCode.INVALID_CURSOR,
    ),
    **LIMIT_ERRORS,
}


@users_router.get(
    "/{ref}/followers",
    response_model=Page[UserListItem],
    summary="Подписчики человека",
    description=(
        "Страница карточек с полем `relationship` (как вы связаны с каждым), по давности подписки, "
        "новые сверху. Свой список виден всегда; чужой зависит от настройки владельца "
        "`followers_list_visibility` (`403 list_hidden`) и закрытости профиля (`403 profile_private`). "
        "Нет такого человека, блокировка или аккаунт не `active`: `404`. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses=_USER_LIST_ERRORS,
)
async def user_followers_endpoint(
    ref: _REF,
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserListItem]:
    return await list_user_follows(
        uow.session,
        viewer_id=principal.user_id,
        ref=ref,
        followers=True,
        limit=limit,
        cursor=cursor,
    )


@users_router.get(
    "/{ref}/following",
    response_model=Page[UserListItem],
    summary="Подписки человека",
    description=(
        "На кого подписан человек; формат, порядок и ошибки как у подписчиков. Подписки живут под "
        "той же настройкой владельца `followers_list_visibility`: отдельной для них нет. "
        "Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses=_USER_LIST_ERRORS,
)
async def user_following_endpoint(
    ref: _REF,
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserListItem]:
    return await list_user_follows(
        uow.session,
        viewer_id=principal.user_id,
        ref=ref,
        followers=False,
        limit=limit,
        cursor=cursor,
    )


router.include_router(follows_router)
router.include_router(me_router)
router.include_router(users_router)

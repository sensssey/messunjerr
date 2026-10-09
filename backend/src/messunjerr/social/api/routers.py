"""Маршруты социального графа: заявки в друзья, друзья, списки людей, блокировки (5.4, 5.3).

Права и порядок проверок живут в политиках (`social.domain.policies`, 4.6): ручки только вызывают
команды и запросы. Подписки и запросы на подписку (S8) живут в `social.api.follows`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import AfterValidator

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import UowDep
from messunjerr.core.idempotency import idempotent_route_class
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses
from messunjerr.core.pagination import DEFAULT_LIMIT, CursorParam, Limit, Page
from messunjerr.core.schemas import normalize_search
from messunjerr.identity.api_public import PrincipalDep, limit_user, no_store, principal_user_id
from messunjerr.profiles.api_public import UserSummary
from messunjerr.social.api.follows import router as follow_router
from messunjerr.social.api.schemas import SendFriendRequestBody
from messunjerr.social.api.search import router as search_router
from messunjerr.social.commands.blocks import BlockUser, UnblockUser, block_user, unblock_user
from messunjerr.social.commands.friend_requests import (
    RespondToRequest,
    SendFriendRequest,
    accept_friend_request,
    cancel_friend_request,
    decline_friend_request,
    send_friend_request,
)
from messunjerr.social.commands.friends import RemoveFriend, remove_friend
from messunjerr.social.domain.rules import Direction
from messunjerr.social.queries.blocks import list_blocks
from messunjerr.social.queries.friend_requests import list_friend_requests
from messunjerr.social.queries.friends import list_my_friends
from messunjerr.social.queries.models import (
    AcceptedFriend,
    BlockEntry,
    FriendEntry,
    FriendRequest,
    UserListItem,
)
from messunjerr.social.queries.user_lists import user_friends, user_mutual_friends

_AUTH_ERRORS = {**TOKEN_ERRORS, **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING)}
"""Ошибки токена и ограничение аккаунта, который ждёт удаления (5.1)."""

_REQUEST_ID = Annotated[uuid.UUID, Path(description="Идентификатор заявки в друзья.")]
_USER_ID = Annotated[uuid.UUID, Path(description="Идентификатор человека.")]
_REF = Annotated[str, Path(description="UUID или ник.")]

# `Idempotency-Key` принимает только создание заявки (5.4): остальные ответы идемпотентны сами по себе.
create_router = APIRouter(
    prefix="/friend-requests",
    tags=["social"],
    route_class=idempotent_route_class(principal_user_id),
    dependencies=[Depends(no_store)],
)
requests_router = APIRouter(
    prefix="/friend-requests", tags=["social"], dependencies=[Depends(no_store)]
)
friends_router = APIRouter(prefix="/friends", tags=["social"], dependencies=[Depends(no_store)])
users_router = APIRouter(prefix="/users", tags=["social"], dependencies=[Depends(no_store)])
blocks_router = APIRouter(tags=["social"], dependencies=[Depends(no_store)])


# ----------------------------------------------------------------------------- заявки в друзья
@create_router.post(
    "",
    status_code=201,
    response_model=FriendRequest,
    summary="Отправить заявку в друзья",
    description=(
        "`201` и заявка `pending`. Если человек уже отправил вам заявку, она принимается сразу: "
        "`200` и `status: accepted` (две заявки друг другу в один миг дают ровно одну дружбу). Цель "
        "не `active`, блокировка в любую сторону или нет такого человека: `404`. Принимает "
        "`Idempotency-Key`. Лимит `friend_request` (30 в сутки)."
    ),
    dependencies=[Depends(limit_user("friend_request", "api_write"))],
    responses={
        200: {
            "model": FriendRequest,
            "description": "Встречная заявка принята сразу: та же заявка со статусом `accepted`.",
        },
        **_AUTH_ERRORS,
        **problem_responses(
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.SELF_ACTION,
            ErrorCode.NOT_FOUND,
            ErrorCode.ALREADY_FRIENDS,
            ErrorCode.FRIEND_REQUEST_EXISTS,
            ErrorCode.IDEMPOTENCY_KEY_REUSE,
            ErrorCode.REQUEST_IN_PROGRESS,
        ),
        **LIMIT_ERRORS,
    },
)
async def send_friend_request_endpoint(
    body: SendFriendRequestBody, response: Response, principal: PrincipalDep, uow: UowDep
) -> FriendRequest:
    result = await send_friend_request(
        SendFriendRequest(sender_id=principal.user_id, receiver_id=body.user_id), uow=uow
    )
    if not result.created:
        response.status_code = 200
    return result.request


@requests_router.get(
    "",
    response_model=Page[FriendRequest],
    summary="Заявки в друзья",
    description=(
        "Активные заявки, новые сверху. `direction=incoming` (по умолчанию) пришли вам, `outgoing` "
        "отправили вы; `user` это собеседник. Аккаунты не `active` не показываются. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def list_friend_requests_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    direction: Annotated[Direction, Query(description="Чьи заявки показать.")] = Direction.INCOMING,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[FriendRequest]:
    return await list_friend_requests(
        uow.session, user_id=principal.user_id, direction=direction, limit=limit, cursor=cursor
    )


@requests_router.post(
    "/{request_id}/accept",
    response_model=AcceptedFriend,
    summary="Принять заявку",
    description=(
        "Только получатель. `200`: новый друг и дата дружбы. Чужая заявка, несуществующая или от "
        "аккаунта не `active`: `404`. Заявка уже получила ответ: `409 friend_request_not_pending`. "
        "Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.FRIEND_REQUEST_NOT_PENDING),
        **LIMIT_ERRORS,
    },
)
async def accept_friend_request_endpoint(
    request_id: _REQUEST_ID, principal: PrincipalDep, uow: UowDep
) -> AcceptedFriend:
    return await accept_friend_request(
        RespondToRequest(actor_id=principal.user_id, request_id=request_id), uow=uow
    )


@requests_router.post(
    "/{request_id}/decline",
    status_code=204,
    response_class=Response,
    summary="Отклонить заявку",
    description=(
        "Только получатель; отправитель об отказе не уведомляется. Ошибки как у принятия. "
        "Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.FRIEND_REQUEST_NOT_PENDING),
        **LIMIT_ERRORS,
    },
)
async def decline_friend_request_endpoint(
    request_id: _REQUEST_ID, principal: PrincipalDep, uow: UowDep
) -> None:
    await decline_friend_request(
        RespondToRequest(actor_id=principal.user_id, request_id=request_id), uow=uow
    )


@requests_router.delete(
    "/{request_id}",
    status_code=204,
    response_class=Response,
    summary="Отменить свою заявку",
    description="Только отправитель. Ошибки: `404`, `409 friend_request_not_pending`. Лимит `api_write`.",
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.FRIEND_REQUEST_NOT_PENDING),
        **LIMIT_ERRORS,
    },
)
async def cancel_friend_request_endpoint(
    request_id: _REQUEST_ID, principal: PrincipalDep, uow: UowDep
) -> None:
    await cancel_friend_request(
        RespondToRequest(actor_id=principal.user_id, request_id=request_id), uow=uow
    )


# ----------------------------------------------------------------------------- друзья
@friends_router.get(
    "",
    response_model=Page[FriendEntry],
    summary="Мои друзья",
    description=(
        "По давности дружбы, новые сверху. `q`: поиск по нику и имени среди друзей (подстрока без учёта "
        "регистра). Аккаунты не `active` не показываются. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def list_friends_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    q: Annotated[
        str | None,
        Query(max_length=100, description="Часть ника или имени среди друзей."),
        AfterValidator(normalize_search),
    ] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[FriendEntry]:
    return await list_my_friends(
        uow.session,
        user_id=principal.user_id,
        q=q,
        limit=limit,
        cursor=cursor,
    )


@friends_router.delete(
    "/{user_id}",
    status_code=204,
    response_class=Response,
    summary="Удалить из друзей",
    description="Дружба заканчивается для обоих. Не друзья: `404`. Лимит `api_write`.",
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **problem_responses(ErrorCode.NOT_FOUND), **LIMIT_ERRORS},
)
async def remove_friend_endpoint(user_id: _USER_ID, principal: PrincipalDep, uow: UowDep) -> None:
    await remove_friend(RemoveFriend(actor_id=principal.user_id, friend_id=user_id), uow=uow)


# ----------------------------------------------------------------------------- списки чужого профиля
@users_router.get(
    "/{ref}/friends",
    response_model=Page[UserListItem],
    summary="Друзья человека",
    description=(
        "Страница карточек с полем `relationship` (как вы связаны с каждым), по давности дружбы. Свой "
        "список виден всегда; чужой зависит от настройки владельца (`403 list_hidden`) и закрытости "
        "профиля (`403 profile_private`). Нет такого человека, блокировка или аккаунт не `active`: "
        "`404`. Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(
            ErrorCode.NOT_FOUND,
            ErrorCode.LIST_HIDDEN,
            ErrorCode.PROFILE_PRIVATE,
            ErrorCode.INVALID_CURSOR,
        ),
        **LIMIT_ERRORS,
    },
)
async def user_friends_endpoint(
    ref: _REF,
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserListItem]:
    return await user_friends(
        uow.session, viewer_id=principal.user_id, ref=ref, limit=limit, cursor=cursor
    )


@users_router.get(
    "/{ref}/mutual-friends",
    response_model=Page[UserSummary],
    summary="Общие друзья",
    description=(
        "Люди, которые друзья и вам, и владельцу профиля. У своего профиля страница пустая. Ошибки "
        "только `404` (нет человека, блокировка, аккаунт не `active`). Лимит `api_read`."
    ),
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.NOT_FOUND, ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def user_mutual_friends_endpoint(
    ref: _REF,
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[UserSummary]:
    return await user_mutual_friends(
        uow.session, viewer_id=principal.user_id, ref=ref, limit=limit, cursor=cursor
    )


# ----------------------------------------------------------------------------- блокировки
@blocks_router.get(
    "/me/blocks",
    response_model=Page[BlockEntry],
    summary="Мои блокировки",
    description="Кого вы заблокировали, новые сверху. Лимит `api_read`.",
    dependencies=[Depends(limit_user("api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.INVALID_CURSOR),
        **LIMIT_ERRORS,
    },
)
async def list_blocks_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: CursorParam = None,
) -> Page[BlockEntry]:
    return await list_blocks(uow.session, user_id=principal.user_id, limit=limit, cursor=cursor)


@blocks_router.put(
    "/blocks/{user_id}",
    status_code=204,
    response_class=Response,
    summary="Заблокировать человека",
    description=(
        "Идемпотентно. В одной транзакции исчезают дружба и подписки в обе стороны, отменяются "
        "активная заявка в друзья и ждущие запросы на подписку; дальше вы не видите друг друга "
        "(`404`). Нет такого человека, аккаунт не `active` или он сам вас заблокировал (для вас он "
        "скрыт): `404`; взаимной блокировки не бывает. Лимит `api_write`."
    ),
    dependencies=[Depends(limit_user("api_write"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.SELF_ACTION, ErrorCode.NOT_FOUND),
        **LIMIT_ERRORS,
    },
)
async def block_user_endpoint(user_id: _USER_ID, principal: PrincipalDep, uow: UowDep) -> None:
    await block_user(BlockUser(actor_id=principal.user_id, target_id=user_id), uow=uow)


@blocks_router.delete(
    "/blocks/{user_id}",
    status_code=204,
    response_class=Response,
    summary="Снять блокировку",
    description="Идемпотентно; дружба и заявки не восстанавливаются. Лимит `api_write`.",
    dependencies=[Depends(limit_user("api_write"))],
    responses={**_AUTH_ERRORS, **LIMIT_ERRORS},
)
async def unblock_user_endpoint(user_id: _USER_ID, principal: PrincipalDep, uow: UowDep) -> None:
    await unblock_user(UnblockUser(actor_id=principal.user_id, target_id=user_id), uow=uow)


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(search_router)
api_router.include_router(create_router)
api_router.include_router(requests_router)
api_router.include_router(friends_router)
api_router.include_router(users_router)
api_router.include_router(blocks_router)
api_router.include_router(follow_router)

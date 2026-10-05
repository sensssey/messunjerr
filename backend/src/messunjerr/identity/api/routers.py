"""Маршруты identity: `/auth/*`, `/me`, `/legal/documents`, `/.well-known/jwks.json` (5.2, 5.3).

Роутеры тонкие: разбирают вход, вызывают команду или запрос и формируют ответ (4.3).
"""

import uuid
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Query, Request, Response, status

from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode
from messunjerr.core.csrf import require_csrf_guard
from messunjerr.core.deps import ResourcesDep, UowDep
from messunjerr.core.errors import DomainError
from messunjerr.core.openapi import problem_responses
from messunjerr.core.ratelimit_deps import client_ip, enforce, limit_by_ip, subject_digest
from messunjerr.identity.api.cookies import (
    REFRESH_COOKIE,
    clear_refresh_cookie,
    refresh_cookie_clearing_header,
    set_refresh_cookie,
)
from messunjerr.identity.api.deps import (
    ClientDep,
    IdentityDep,
    PrincipalDep,
    SensitivePrincipalDep,
    limit_user,
    no_store,
)
from messunjerr.identity.api.schemas import (
    AcceptedResponse,
    AuthResponse,
    ChangeEmailRequest,
    ChangePasswordRequest,
    ConfirmationSentResponse,
    ConfirmEmailRequest,
    ForgotPasswordRequest,
    JwkResponse,
    JwksResponse,
    LegalDocument,
    LegalDocumentsResponse,
    LegalOperator,
    LoginRequest,
    LogoutAllRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SessionsResponse,
    UsernameAvailabilityResponse,
    VerifyEmailRequest,
)
from messunjerr.identity.commands.common import SignedIn
from messunjerr.identity.commands.email_change import (
    RequestEmailChange,
    confirm_email_change,
    request_email_change,
)
from messunjerr.identity.commands.login import Login, login
from messunjerr.identity.commands.logout import Actor, logout, logout_all, revoke_session
from messunjerr.identity.commands.password import (
    ChangePassword,
    ResetPassword,
    change_password,
    request_password_reset_after_response,
    reset_password,
)
from messunjerr.identity.commands.refresh import Refresh, refresh_session
from messunjerr.identity.commands.register import RegisterUser, register_user
from messunjerr.identity.commands.resend_verification import resend_verification_after_response
from messunjerr.identity.commands.verify_email import VerifyEmail, verify_email
from messunjerr.identity.domain.errors import unauthorized
from messunjerr.identity.queries.me import check_username_available, get_me
from messunjerr.identity.queries.models import MeUser
from messunjerr.identity.queries.sessions import list_sessions

auth_router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(no_store)])
me_router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(no_store)])
legal_router = APIRouter(prefix="/legal", tags=["legal"])
well_known_router = APIRouter(prefix="/.well-known", tags=["service"])

_TOKEN_ERRORS = problem_responses(
    ErrorCode.TOKEN_MISSING,
    ErrorCode.TOKEN_INVALID,
    ErrorCode.TOKEN_EXPIRED,
    ErrorCode.SESSION_REVOKED,
)
_LIMIT_ERRORS = problem_responses(ErrorCode.RATE_LIMITED, ErrorCode.SERVICE_UNAVAILABLE)

TERMS_TITLE = "Пользовательское соглашение и согласие на обработку персональных данных"
_REFRESH_COOKIE_ERRORS = frozenset(
    {ErrorCode.REFRESH_INVALID, ErrorCode.REFRESH_EXPIRED, ErrorCode.REFRESH_REUSED}
)

RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE, include_in_schema=False)]


def _auth_response(response: Response, signed_in: SignedIn) -> AuthResponse:
    """Тело входа и cookie с refresh-токеном (токен в тело не попадает)."""
    grant = signed_in.grant
    set_refresh_cookie(response, grant.refresh_token, max_age=grant.refresh_max_age)
    return AuthResponse(
        access_token=grant.access_token,
        expires_in=grant.expires_in,
        session_id=grant.session_id,
        user=signed_in.user,
    )


# ----------------------------------------------------------------------------- регистрация и почта
@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Регистрация",
    description=(
        "Создаёт аккаунт в статусе `pending` и отправляет письмо с токеном подтверждения. "
        "Ответ одинаков, даже если адрес уже занят (тогда владельцу уходит письмо «вы уже "
        "зарегистрированы»). ⚖️ Нужна галочка согласия `accept_terms`. Поля `display_name`, "
        "`language` и `timezone` появятся вместе с профилем (S3). Лимит `auth_register_ip`."
    ),
    dependencies=[Depends(limit_by_ip("auth_register_ip"))],
    responses={
        **problem_responses(ErrorCode.VALIDATION_ERROR, ErrorCode.USERNAME_TAKEN),
        **_LIMIT_ERRORS,
    },
)
async def register(
    body: RegisterRequest, uow: UowDep, identity: IdentityDep, resources: ResourcesDep
) -> RegisterResponse:
    await register_user(
        RegisterUser(
            email=body.email,
            username=body.username,
            password=body.password,
            accept_terms=body.accept_terms,
        ),
        uow=uow,
        passwords=identity.passwords,
        jobs=resources.jobs,
        settings=resources.settings,
    )
    return RegisterResponse()


@auth_router.post(
    "/verify-email",
    response_model=AuthResponse,
    summary="Подтверждение почты",
    description=(
        "Токен из письма одноразовый и действует 24 часа. В ответ выполняется вход: тело как у "
        "`/auth/login` и cookie `__Secure-mj_refresh`. Лимит `auth_email_ip`."
    ),
    dependencies=[Depends(limit_by_ip("auth_email_ip"))],
    responses={
        **problem_responses(
            ErrorCode.TOKEN_INVALID_OR_EXPIRED,
            ErrorCode.ACCOUNT_SUSPENDED,
            ErrorCode.ACCOUNT_BANNED,
            ErrorCode.VALIDATION_ERROR,
        ),
        **_LIMIT_ERRORS,
    },
)
async def verify_email_endpoint(
    body: VerifyEmailRequest,
    response: Response,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> AuthResponse:
    signed_in = await verify_email(
        VerifyEmail(token=body.token, client=client),
        uow=uow,
        tokens=identity.tokens,
        settings=resources.settings,
    )
    return _auth_response(response, signed_in)


@auth_router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Повторное письмо подтверждения",
    description=(
        "Ответ `202` не зависит от того, есть ли адрес и подтверждён ли он. "
        "Лимиты `auth_email_ip` и `auth_email_addr`."
    ),
    responses={**problem_responses(ErrorCode.VALIDATION_ERROR), **_LIMIT_ERRORS},
)
async def resend_verification_endpoint(
    body: ResendVerificationRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    resources: ResourcesDep,
) -> AcceptedResponse:
    await enforce(
        resources.limiter,
        [("auth_email_ip", client_ip(request)), ("auth_email_addr", subject_digest(body.email))],
        response,
        request,
    )
    # Работа идёт после ответа: ни время, ни ошибки не выдают, существует ли адрес.
    background.add_task(
        resend_verification_after_response,
        body.email,
        sessionmaker=resources.sessionmaker,
        jobs=resources.jobs,
        settings=resources.settings,
    )
    return AcceptedResponse()


# ----------------------------------------------------------------------------- вход, refresh, выход
@auth_router.post(
    "/login",
    response_model=AuthResponse,
    summary="Вход",
    description=(
        "Вход по почте или нику. Неверная пара и несуществующий логин неразличимы по ответу и по "
        "времени (`invalid_credentials`). Лимиты: `auth_login_ip` (20 за 10 минут с адреса) и "
        "`auth_login_account` (5 неудачных попыток за 15 минут на аккаунт)."
    ),
    dependencies=[Depends(limit_by_ip("auth_login_ip"))],
    responses={
        **problem_responses(
            ErrorCode.INVALID_CREDENTIALS,
            ErrorCode.EMAIL_NOT_VERIFIED,
            ErrorCode.ACCOUNT_SUSPENDED,
            ErrorCode.ACCOUNT_BANNED,
            ErrorCode.VALIDATION_ERROR,
        ),
        **_LIMIT_ERRORS,
    },
)
async def login_endpoint(
    body: LoginRequest,
    response: Response,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> AuthResponse:
    signed_in = await login(
        Login(
            login=body.login,
            password=body.password,
            client=replace(client, device_label=body.device_label),
        ),
        uow=uow,
        passwords=identity.passwords,
        tokens=identity.tokens,
        limiter=resources.limiter,
        settings=resources.settings,
    )
    return _auth_response(response, signed_in)


@auth_router.post(
    "/refresh",
    response_model=AuthResponse,
    summary="Обновление access-токена",
    description=(
        "Без тела: refresh-токен приходит в cookie `__Secure-mj_refresh`, нужен заголовок "
        "`X-Requested-With: messunjerr`. Токен ротируется: в ответе новая cookie. В окне гонки двух "
        "вкладок (10 с после ротации) выдаётся только новый access-токен, `Set-Cookie` нет. Повтор "
        "старого токена позже окна закрывает сессию (`refresh_reused`)."
    ),
    dependencies=[Depends(require_csrf_guard)],
    responses={
        **problem_responses(
            ErrorCode.REFRESH_MISSING,
            ErrorCode.REFRESH_INVALID,
            ErrorCode.REFRESH_EXPIRED,
            ErrorCode.REFRESH_REUSED,
            ErrorCode.CSRF_FAILED,
            ErrorCode.ACCOUNT_SUSPENDED,
            ErrorCode.ACCOUNT_BANNED,
        ),
        **_LIMIT_ERRORS,
    },
)
async def refresh_endpoint(
    response: Response,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
    refresh_cookie: RefreshCookie = None,
) -> AuthResponse:
    try:
        refreshed = await refresh_session(
            Refresh(token=refresh_cookie, client=client),
            uow=uow,
            tokens=identity.tokens,
            denylist=identity.denylist,
            limiter=resources.limiter,
            jobs=resources.jobs,
            settings=resources.settings,
        )
    except DomainError as error:
        if error.code in _REFRESH_COOKIE_ERRORS:
            # Мёртвый токен браузеру больше не нужен: стираем cookie вместе с ответом-ошибкой.
            error.headers["Set-Cookie"] = refresh_cookie_clearing_header()
        raise
    if refreshed.new_refresh_token is not None:
        set_refresh_cookie(response, refreshed.new_refresh_token, max_age=refreshed.refresh_max_age)
    return AuthResponse(
        access_token=refreshed.access_token,
        expires_in=refreshed.expires_in,
        session_id=refreshed.session_id,
        user=refreshed.user,
    )


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Выход",
    description=(
        "Закрывает текущую сессию и стирает cookie. Всегда `204` (идемпотентно); нужен заголовок "
        "`X-Requested-With: messunjerr`."
    ),
    dependencies=[Depends(require_csrf_guard)],
    responses=problem_responses(ErrorCode.CSRF_FAILED),
)
async def logout_endpoint(
    response: Response,
    uow: UowDep,
    identity: IdentityDep,
    client: ClientDep,
    refresh_cookie: RefreshCookie = None,
) -> None:
    await logout(refresh_cookie, uow=uow, denylist=identity.denylist, client=client)
    clear_refresh_cookie(response)


@auth_router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Выйти везде",
    description=(
        "Требует пароль. Закрывает все сессии; с `keep_current: true` текущая остаётся. Чувствительная "
        "операция: без Redis отвечает `503`."
    ),
    dependencies=[Depends(limit_user("api_write", sensitive=True))],
    responses={
        **_TOKEN_ERRORS,
        **problem_responses(ErrorCode.REAUTH_FAILED, ErrorCode.VALIDATION_ERROR),
        **_LIMIT_ERRORS,
    },
)
async def logout_all_endpoint(
    body: LogoutAllRequest,
    response: Response,
    principal: SensitivePrincipalDep,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> None:
    await logout_all(
        actor=Actor(principal.user_id, principal.session_id),
        password=body.password,
        keep_current=body.keep_current,
        uow=uow,
        passwords=identity.passwords,
        limiter=resources.limiter,
        denylist=identity.denylist,
        client=client,
    )
    if not body.keep_current:
        clear_refresh_cookie(response)


@auth_router.get(
    "/sessions",
    response_model=SessionsResponse,
    summary="Активные сессии",
    description="Действующие сессии пользователя; `current` отмечает ту, с которой сделан запрос.",
    dependencies=[Depends(limit_user("api_read", sensitive=True))],
    responses={**_TOKEN_ERRORS, **_LIMIT_ERRORS},
)
async def sessions_endpoint(principal: SensitivePrincipalDep, uow: UowDep) -> SessionsResponse:
    items = await list_sessions(
        uow.session,
        user_id=principal.user_id,
        current_session_id=principal.session_id,
        now=utcnow(),
    )
    return SessionsResponse(items=items)


@auth_router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Закрыть сессию",
    description=(
        "Если закрывается текущая сессия, ведёт себя как `logout` (cookie стирается). Чужая и "
        "несуществующая сессии дают `404`."
    ),
    dependencies=[Depends(limit_user("api_write", sensitive=True))],
    responses={**_TOKEN_ERRORS, **problem_responses(ErrorCode.NOT_FOUND), **_LIMIT_ERRORS},
)
async def delete_session_endpoint(
    session_id: uuid.UUID,
    response: Response,
    principal: SensitivePrincipalDep,
    uow: UowDep,
    identity: IdentityDep,
    client: ClientDep,
) -> None:
    was_current = await revoke_session(
        actor=Actor(principal.user_id, principal.session_id),
        session_id=session_id,
        uow=uow,
        denylist=identity.denylist,
        client=client,
    )
    if was_current:
        clear_refresh_cookie(response)


# ----------------------------------------------------------------------------- пароль
@auth_router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Забыли пароль",
    description=(
        "Ответ `202` всегда одинаков, письмо уходит только существующему аккаунту. Лимиты "
        "`auth_email_ip` и `auth_email_addr`."
    ),
    responses={**problem_responses(ErrorCode.VALIDATION_ERROR), **_LIMIT_ERRORS},
)
async def forgot_password_endpoint(
    body: ForgotPasswordRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    resources: ResourcesDep,
    client: ClientDep,
) -> AcceptedResponse:
    await enforce(
        resources.limiter,
        [("auth_email_ip", client_ip(request)), ("auth_email_addr", subject_digest(body.email))],
        response,
        request,
    )
    background.add_task(
        request_password_reset_after_response,
        body.email,
        sessionmaker=resources.sessionmaker,
        jobs=resources.jobs,
        settings=resources.settings,
        client=client,
    )
    return AcceptedResponse()


@auth_router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Сброс пароля по токену из письма",
    description=(
        "Задаёт новый пароль, закрывает все сессии, подтверждает почту (владелец открыл письмо) и "
        "отправляет уведомление. Автоматического входа нет. Лимит `auth_email_ip`."
    ),
    dependencies=[Depends(limit_by_ip("auth_email_ip"))],
    responses={
        **problem_responses(ErrorCode.TOKEN_INVALID_OR_EXPIRED, ErrorCode.VALIDATION_ERROR),
        **_LIMIT_ERRORS,
    },
)
async def reset_password_endpoint(
    body: ResetPasswordRequest,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> None:
    await reset_password(
        ResetPassword(token=body.token, new_password=body.new_password, client=client),
        uow=uow,
        passwords=identity.passwords,
        denylist=identity.denylist,
        jobs=resources.jobs,
        settings=resources.settings,
    )


@auth_router.post(
    "/password/change",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Смена пароля",
    description=(
        "Требует текущий пароль. Остальные сессии закрываются (`revoke_other_sessions`, по умолчанию "
        "да), текущая остаётся. Чувствительная операция: без Redis отвечает `503`."
    ),
    dependencies=[Depends(limit_user("api_write", sensitive=True))],
    responses={
        **_TOKEN_ERRORS,
        **problem_responses(ErrorCode.REAUTH_FAILED, ErrorCode.VALIDATION_ERROR),
        **_LIMIT_ERRORS,
    },
)
async def change_password_endpoint(
    body: ChangePasswordRequest,
    principal: SensitivePrincipalDep,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> None:
    await change_password(
        ChangePassword(
            actor=Actor(principal.user_id, principal.session_id),
            current_password=body.current_password,
            new_password=body.new_password,
            revoke_other_sessions=body.revoke_other_sessions,
            client=client,
        ),
        uow=uow,
        passwords=identity.passwords,
        limiter=resources.limiter,
        denylist=identity.denylist,
        jobs=resources.jobs,
        settings=resources.settings,
    )


# ----------------------------------------------------------------------------- почта
@auth_router.post(
    "/email/change",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ConfirmationSentResponse,
    summary="Смена почты",
    description=(
        "Требует пароль. Токен уходит на новый адрес, старый получает уведомление. Ответ `202` "
        "одинаков и для занятого адреса: занятость не раскрывается. Чувствительная операция."
    ),
    dependencies=[Depends(limit_user("api_write", sensitive=True))],
    responses={
        **_TOKEN_ERRORS,
        **problem_responses(ErrorCode.REAUTH_FAILED, ErrorCode.VALIDATION_ERROR),
        **_LIMIT_ERRORS,
    },
)
async def change_email_endpoint(
    body: ChangeEmailRequest,
    principal: SensitivePrincipalDep,
    uow: UowDep,
    identity: IdentityDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> ConfirmationSentResponse:
    await request_email_change(
        RequestEmailChange(
            actor=Actor(principal.user_id, principal.session_id),
            new_email=body.new_email,
            password=body.password,
            client=client,
        ),
        uow=uow,
        passwords=identity.passwords,
        limiter=resources.limiter,
        jobs=resources.jobs,
        settings=resources.settings,
    )
    return ConfirmationSentResponse()


@auth_router.post(
    "/email/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Подтверждение новой почты",
    description="Заменяет адрес аккаунта по токену из письма. Сессии остаются. Лимит `auth_email_ip`.",
    dependencies=[Depends(limit_by_ip("auth_email_ip"))],
    responses={
        **problem_responses(ErrorCode.TOKEN_INVALID_OR_EXPIRED, ErrorCode.VALIDATION_ERROR),
        **_LIMIT_ERRORS,
    },
)
async def confirm_email_endpoint(
    body: ConfirmEmailRequest,
    uow: UowDep,
    resources: ResourcesDep,
    client: ClientDep,
) -> None:
    await confirm_email_change(
        body.token, uow=uow, jobs=resources.jobs, settings=resources.settings, client=client
    )


# ----------------------------------------------------------------------------- прочее
@auth_router.get(
    "/username-available",
    response_model=UsernameAvailabilityResponse,
    summary="Свободен ли ник",
    description="Лимит `username_check_ip`.",
    dependencies=[Depends(limit_by_ip("username_check_ip"))],
    responses={**problem_responses(ErrorCode.VALIDATION_ERROR), **_LIMIT_ERRORS},
)
async def username_available(
    uow: UowDep, username: Annotated[str, Query(min_length=1, max_length=100)]
) -> UsernameAvailabilityResponse:
    result = await check_username_available(uow.session, username)
    return UsernameAvailabilityResponse(available=result.available, reason=result.reason)


@me_router.get(
    "",
    response_model=MeUser,
    summary="Текущий пользователь",
    description="Профиль, настройки приватности и счётчики добавятся в S3. Лимит `api_read`.",
    dependencies=[Depends(limit_user("api_read"))],
    responses={**_TOKEN_ERRORS, **_LIMIT_ERRORS},
)
async def read_me(principal: PrincipalDep, uow: UowDep) -> MeUser:
    me = await get_me(uow.session, principal.user_id)
    if me is None:
        # Токен подписан нами, но пользователя уже нет (удалён): для клиента это то же, что неверный токен.
        raise unauthorized(ErrorCode.TOKEN_INVALID, "The user of this token no longer exists.")
    return me


@legal_router.get(
    "/documents",
    response_model=LegalDocumentsResponse,
    summary="Юридические документы",
    description=(
        "⚖️ Версия условий, которую человек принимает галочкой при регистрации, и сведения об "
        "операторе. Полный реестр документов и согласий в бэклоге (B-01)."
    ),
)
async def legal_documents(resources: ResourcesDep, response: Response) -> LegalDocumentsResponse:
    settings = resources.settings
    response.headers["Cache-Control"] = "public, max-age=300"
    return LegalDocumentsResponse(
        operator=LegalOperator(
            name=settings.legal_operator_name,
            address=settings.legal_operator_address,
            contact_email=settings.legal_contact_email,
        ),
        min_age=settings.min_age,
        items=[
            LegalDocument(
                slug="terms",
                version=settings.legal_terms_version,
                title=TERMS_TITLE,
                url="/legal/terms",
            )
        ],
    )


@well_known_router.get(
    "/jwks.json",
    response_model=JwksResponse,
    summary="Открытые ключи для проверки access-токенов (JWKS)",
)
async def jwks(identity: IdentityDep, response: Response) -> JwksResponse:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return JwksResponse(keys=[JwkResponse(**key) for key in identity.tokens.jwks()["keys"]])


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(me_router)
api_router.include_router(legal_router)

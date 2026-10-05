"""Маршруты identity: `/auth/*`, `/me`, `/legal/documents`, `/.well-known/jwks.json` (5.2, 5.3).

Роутеры тонкие: разбирают вход, вызывают команду или запрос и формируют ответ (4.3).
"""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep, UowDep
from messunjerr.core.openapi import problem_responses
from messunjerr.identity.api.cookies import set_refresh_cookie
from messunjerr.identity.api.deps import (
    ClientDep,
    IdentityDep,
    PrincipalDep,
    no_store,
)
from messunjerr.identity.api.schemas import (
    AcceptedResponse,
    AuthResponse,
    JwkResponse,
    JwksResponse,
    LegalDocument,
    LegalDocumentsResponse,
    LegalOperator,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    UsernameAvailabilityResponse,
    VerifyEmailRequest,
)
from messunjerr.identity.commands.common import SignedIn
from messunjerr.identity.commands.login import Login, login
from messunjerr.identity.commands.register import RegisterUser, register_user
from messunjerr.identity.commands.resend_verification import resend_verification_after_response
from messunjerr.identity.commands.verify_email import VerifyEmail, verify_email
from messunjerr.identity.domain.errors import unauthorized
from messunjerr.identity.queries.me import check_username_available, get_me
from messunjerr.identity.queries.models import MeUser

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

TERMS_TITLE = "Пользовательское соглашение и согласие на обработку персональных данных"


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


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Регистрация",
    description=(
        "Создаёт аккаунт в статусе `pending` и отправляет письмо с токеном подтверждения. "
        "Ответ одинаков, даже если адрес уже занят (тогда владельцу уходит письмо «вы уже "
        "зарегистрированы»). ⚖️ Нужна галочка согласия `accept_terms`. Поля `display_name`, "
        "`language` и `timezone` появятся вместе с профилем (S3)."
    ),
    responses=problem_responses(
        ErrorCode.VALIDATION_ERROR, ErrorCode.USERNAME_TAKEN, ErrorCode.SERVICE_UNAVAILABLE
    ),
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
        "`/auth/login` и cookie `__Secure-mj_refresh`."
    ),
    responses=problem_responses(
        ErrorCode.TOKEN_INVALID_OR_EXPIRED,
        ErrorCode.ACCOUNT_SUSPENDED,
        ErrorCode.ACCOUNT_BANNED,
        ErrorCode.VALIDATION_ERROR,
    ),
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
    description="Ответ `202` не зависит от того, есть ли адрес и подтверждён ли он.",
    responses=problem_responses(ErrorCode.VALIDATION_ERROR),
)
async def resend_verification_endpoint(
    body: ResendVerificationRequest, background: BackgroundTasks, resources: ResourcesDep
) -> AcceptedResponse:
    # Работа идёт после ответа: ни время, ни ошибки не выдают, существует ли адрес.
    background.add_task(
        resend_verification_after_response,
        body.email,
        sessionmaker=resources.sessionmaker,
        jobs=resources.jobs,
        settings=resources.settings,
    )
    return AcceptedResponse()


@auth_router.post(
    "/login",
    response_model=AuthResponse,
    summary="Вход",
    description=(
        "Вход по почте или нику. Неверная пара и несуществующий логин неразличимы по ответу и по "
        "времени (`invalid_credentials`)."
    ),
    responses=problem_responses(
        ErrorCode.INVALID_CREDENTIALS,
        ErrorCode.EMAIL_NOT_VERIFIED,
        ErrorCode.ACCOUNT_SUSPENDED,
        ErrorCode.ACCOUNT_BANNED,
        ErrorCode.VALIDATION_ERROR,
    ),
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
        settings=resources.settings,
    )
    return _auth_response(response, signed_in)


@auth_router.get(
    "/username-available",
    response_model=UsernameAvailabilityResponse,
    summary="Свободен ли ник",
    responses=problem_responses(ErrorCode.VALIDATION_ERROR),
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
    description="Профиль, настройки приватности и счётчики добавятся в S3.",
    responses=_TOKEN_ERRORS,
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

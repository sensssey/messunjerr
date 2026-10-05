"""Схемы запросов и ответов identity (5.2, 5.3)."""

import uuid
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool
from pydantic_core import PydanticCustomError

from messunjerr.core.codes import ItemCode
from messunjerr.core.fields import DisplayName, Language, Timezone
from messunjerr.core.limits import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from messunjerr.core.schemas import RAW, ApiModel, UtcDateTime
from messunjerr.identity.domain.emails import EmailProblem, InvalidEmailError, normalize_email
from messunjerr.identity.domain.usernames import USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH
from messunjerr.identity.queries.models import MeUser
from messunjerr.identity.queries.sessions import SessionInfo


def _valid_email(value: str) -> str:
    try:
        return normalize_email(value)
    except InvalidEmailError as error:
        code = (
            ItemCode.STRING_TOO_LONG
            if error.problem is EmailProblem.TOO_LONG
            else ItemCode.INVALID_FORMAT
        )
        raise PydanticCustomError(code.value, "Enter a valid email address.") from error


Email = Annotated[str, AfterValidator(_valid_email)]
"""Адрес приводится к виду хранения (нижний регистр), длина ≤ 254."""

Username = Annotated[
    str,
    Field(
        min_length=USERNAME_MIN_LENGTH, max_length=USERNAME_MAX_LENGTH, pattern=r"^[A-Za-z0-9_]+$"
    ),
    AfterValidator(str.lower),
]
"""Ник: `A–Z a–z 0–9 _`, регистр игнорируется, сохраняется в нижнем."""

Password = Annotated[
    str, RAW, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
]
"""Пароль не нормализуется и не обрезается: пробелы по краям считаются частью пароля."""


class RegisterRequest(ApiModel):
    email: Email
    username: Username
    password: Password
    display_name: DisplayName | None = Field(
        default=None, description="Имя для показа, 1–50 символов; по умолчанию равно нику."
    )
    language: Language | None = Field(
        default=None, description="Язык интерфейса, BCP 47 (`ru`, `en-US`)."
    )
    timezone: Timezone | None = Field(
        default=None, description="Часовой пояс из базы IANA (`Europe/Moscow`)."
    )
    accept_terms: Annotated[
        StrictBool,
        Field(
            default=False,
            description=(
                "⚖️ Галочка «Принимаю условия и даю согласие на обработку персональных данных; "
                "мне есть 18 лет». Без `true` регистрация невозможна (`consent_missing`). "
                "Решает команда, а не схема: так `consent_missing` приходит в одном ответе вместе "
                "с остальными ошибками правил (зарезервированный ник, слабый пароль)."
            ),
        ),
    ]


class VerifyEmailRequest(ApiModel):
    token: Annotated[str, Field(min_length=1, max_length=256)]


class ResendVerificationRequest(ApiModel):
    email: Email


class LoginRequest(ApiModel):
    login: Annotated[str, Field(min_length=1, max_length=254, description="Почта или ник.")]
    password: Annotated[str, RAW, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
    device_label: Annotated[str, Field(max_length=100)] | None = None


class LogoutAllRequest(ApiModel):
    password: Annotated[str, RAW, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
    keep_current: StrictBool = Field(
        default=False, description="Оставить текущую сессию (по умолчанию закрываются все)."
    )


class ForgotPasswordRequest(ApiModel):
    email: Email


class ResetPasswordRequest(ApiModel):
    token: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Password


class ChangePasswordRequest(ApiModel):
    current_password: Annotated[str, RAW, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
    new_password: Password
    revoke_other_sessions: StrictBool = Field(
        default=True, description="Закрыть остальные сессии (текущая остаётся)."
    )


class ChangeEmailRequest(ApiModel):
    new_email: Email
    password: Annotated[str, RAW, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class ConfirmEmailRequest(ApiModel):
    token: Annotated[str, Field(min_length=1, max_length=256)]


class ChangeUsernameRequest(ApiModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"username": "ivan_petrov"}]})

    username: Username


class UsernameResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"username": "ivan_petrov"}]})

    username: str


class DeleteAccountRequest(ApiModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"password": "correct horse battery staple"}]}
    )

    password: Annotated[
        str | None,
        RAW,
        Field(
            default=None,
            min_length=1,
            max_length=PASSWORD_MAX_LENGTH,
            description=(
                "Текущий пароль. Аккаунту без пароля (только вход через OAuth) он не нужен: хватает "
                "сессии не старше пяти минут."
            ),
        ),
    ]


class DeletionScheduledResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"deletion_scheduled_at": "2026-10-19T12:34:56.789Z"}]}
    )

    deletion_scheduled_at: UtcDateTime = Field(
        description="Когда аккаунт и данные будут удалены, если его не восстановить."
    )


class SessionsResponse(BaseModel):
    items: list[SessionInfo]


class ConfirmationSentResponse(BaseModel):
    status: Literal["confirmation_sent"] = "confirmation_sent"


class RegisterResponse(BaseModel):
    status: Literal["verification_sent"] = "verification_sent"


class AcceptedResponse(BaseModel):
    status: Literal["accepted"] = "accepted"


class AuthResponse(BaseModel):
    """Ответ входа (5.2). Refresh-токен приходит только в cookie `__Secure-mj_refresh`."""

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 (тип токена по RFC 6750, не секрет)
    expires_in: int = Field(description="Срок жизни access-токена в секундах.")
    session_id: uuid.UUID
    user: MeUser


class UsernameAvailabilityResponse(BaseModel):
    available: bool
    reason: Literal["taken", "reserved", "invalid"] | None = None


class JwkResponse(BaseModel):
    kty: str
    crv: str
    x: str
    kid: str
    use: str
    alg: str


class JwksResponse(BaseModel):
    keys: list[JwkResponse]


class LegalOperator(BaseModel):
    name: str | None
    address: str | None
    contact_email: str | None


class LegalDocument(BaseModel):
    slug: str
    version: str
    title: str
    url: str = Field(description="Путь страницы документа в клиенте.")


class LegalDocumentsResponse(BaseModel):
    operator: LegalOperator
    min_age: int
    items: list[LegalDocument]

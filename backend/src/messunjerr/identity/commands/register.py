"""Команда `POST /auth/register` (5.2).

Ответ клиенту одинаков, существует адрес или нет (4.14, перечисление пользователей):

- адреса нет: создаётся аккаунт `pending`, уходит письмо с токеном подтверждения;
- адрес есть, аккаунт не подтверждён: данные регистрации заменяют прежние, прежние токены гаснут,
  письмо с новым токеном. Иначе тот, кто первым занял чужую почту, оставил бы на ней свой пароль, а
  настоящий владелец подтвердил бы такой аккаунт (pre-hijacking);
- адрес есть, аккаунт активен: ничего не меняется, владельцу уходит письмо «вы уже зарегистрированы».

Во всех ветках считается один и тот же Argon2id-хэш, поэтому и время ответа не выдаёт ветку. Занятый
ник отвечает `409` всегда, независимо от почты.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.exc import IntegrityError

from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode, ItemCode
from messunjerr.core.errors import DomainError, ErrorItem
from messunjerr.core.ids import uuid7
from messunjerr.core.jobs import JobQueue
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import password_policy_error
from messunjerr.identity.commands.mail import issue_verification_email, send_account_exists_email
from messunjerr.identity.domain.errors import username_taken
from messunjerr.identity.domain.events import UserRegistered, record
from messunjerr.identity.domain.passwords import check_password_policy
from messunjerr.identity.domain.usernames import UsernameProblem, check_username
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.ports import ProfileProvisioner, ProfileSeed
from messunjerr.identity.infra.repositories import UserRepository, violated_constraint
from messunjerr.identity.queries.me import username_is_reserved
from messunjerr.settings import Settings


class RegisterOutcome(StrEnum):
    """Что произошло на самом деле. Клиент этого не видит, нужно тестам и журналу."""

    CREATED = "created"
    PENDING_REPLACED = "pending_replaced"
    EXISTS = "exists"


@dataclass(frozen=True, slots=True)
class RegisterUser:
    email: str
    username: str
    password: str
    accept_terms: bool
    display_name: str | None = None
    language: str | None = None
    timezone: str | None = None

    @property
    def profile_seed(self) -> ProfileSeed:
        return ProfileSeed(
            display_name=self.display_name, language=self.language, timezone=self.timezone
        )


class _RaceError(Exception):
    """Адрес или ник успел занять параллельный запрос: нужно перечитать состояние и выбрать ветку."""

    def __init__(self, constraint: str) -> None:
        super().__init__(constraint)
        self.constraint = constraint


def _validate(command: RegisterUser) -> None:
    items: list[ErrorItem] = []
    if not command.accept_terms:
        items.append(
            ErrorItem(
                "/body/accept_terms", ItemCode.CONSENT_MISSING, "Accepting the terms is required."
            )
        )
    match check_username(command.username):
        case UsernameProblem.RESERVED:
            items.append(
                ErrorItem(
                    "/body/username", ItemCode.USERNAME_RESERVED, "This username is reserved."
                )
            )
        case UsernameProblem.INVALID:
            items.append(
                ErrorItem(
                    "/body/username", ItemCode.INVALID_FORMAT, "Use 3-30 letters, digits or _."
                )
            )
        case None:
            pass
    problem = check_password_policy(
        command.password, username=command.username, email=command.email
    )
    if problem is not None:
        items.append(password_policy_error(problem, "/body/password"))
    if items:
        raise DomainError(ErrorCode.VALIDATION_ERROR, errors=items)


async def _register_once(
    command: RegisterUser,
    *,
    uow: UnitOfWork,
    password_hash: str,
    settings: Settings,
    now: datetime,
) -> tuple[RegisterOutcome, UserRow]:
    users = UserRepository(uow.session)
    existing = await users.get_by_email(command.email, for_update=True)
    owner = await users.get_by_username(command.username)
    own_pending_username = (
        existing is not None
        and existing.status == "pending"
        and owner is not None
        and owner.id == existing.id
    )
    if owner is not None and not own_pending_username:
        raise username_taken()
    if owner is None and await username_is_reserved(uow.session, command.username, now=now):
        raise username_taken()  # прежний ник после чьей-то смены остаётся занятым (5.3)

    try:
        # Точка сохранения: нарушение уникальности не должно ломать всю транзакцию.
        async with uow.session.begin_nested():
            if existing is None:
                user = UserRow(
                    id=uuid7(),
                    email=command.email,
                    username=command.username,
                    password_hash=password_hash,
                    terms_version=settings.legal_terms_version,
                    terms_accepted_at=now,
                )
                users.add(user)
                outcome = RegisterOutcome.CREATED
            elif existing.status == "pending":
                existing.username = command.username
                existing.password_hash = password_hash
                existing.terms_version = settings.legal_terms_version
                existing.terms_accepted_at = now
                user = existing
                outcome = RegisterOutcome.PENDING_REPLACED
            else:
                return RegisterOutcome.EXISTS, existing
            await uow.session.flush()
    except IntegrityError as error:
        constraint = violated_constraint(error)
        if constraint in ("uq_users_username", "uq_users_email"):
            raise _RaceError(constraint) from error
        raise
    return outcome, user


async def register_user(
    command: RegisterUser,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    provisioner: ProfileProvisioner,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> RegisterOutcome:
    _validate(command)
    moment = now or utcnow()
    # Хэш считается до обращения к БД и во всех ветках: стоимость и время не зависят от ветки.
    password_hash = await passwords.hash(command.password)

    try:
        outcome, user = await _register_once(
            command, uow=uow, password_hash=password_hash, settings=settings, now=moment
        )
    except _RaceError:
        # Параллельный запрос успел занять адрес или ник: теперь это видно, и ветка выбирается заново.
        # Двойной клик по кнопке даёт «аккаунт уже ждёт подтверждения», а не ложное «ник занят».
        try:
            outcome, user = await _register_once(
                command, uow=uow, password_hash=password_hash, settings=settings, now=moment
            )
        except _RaceError as second:
            if second.constraint == "uq_users_username":
                raise username_taken() from second
            raise

    if outcome is not RegisterOutcome.EXISTS:
        # Профиль создаётся в той же транзакции, что и аккаунт: строки без пары невозможны (S3-01).
        await provisioner.provision(
            uow.session, user_id=user.id, username=user.username, seed=command.profile_seed
        )
    if outcome is RegisterOutcome.CREATED:
        record(uow.outbox, UserRegistered(user.id))
    if outcome is RegisterOutcome.EXISTS:
        await send_account_exists_email(jobs=jobs, settings=settings, user=user)
    else:
        await issue_verification_email(
            session=uow.session, jobs=jobs, settings=settings, user=user, now=moment
        )
    await uow.commit()
    return outcome

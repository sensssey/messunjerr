"""Первый администратор: `messunjerr create-admin` (S6-07, спецификация 4.1).

Обычная регистрация даёт роль `user`, а повысить себя через API нельзя. Администратора заводит
оператор этой командой: создаёт подтверждённый аккаунт с ролью `admin` либо выдаёт роль уже
существующему (по адресу почты); пароль и ник существующего аккаунта она не меняет, а если оператор их
передал, сообщает об этом (`AdminResult.ignored`). Работает в любом окружении, в том числе в `prod`:
ради этого она и нужна. Каждая выдача роли пишется в журнал аудита (`role.changed`), пароль нигде не
печатается.

Ник может быть из зарезервированных (`admin`, `support`…): занять его может только оператор, остальным
эти имена закрыты.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.audit import record_audit
from messunjerr.core.db import create_engine
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.emails import InvalidEmailError, normalize_email
from messunjerr.identity.domain.passwords import check_password_policy
from messunjerr.identity.domain.usernames import USERNAME_PATTERN, normalize_username
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.ports import ProfileSeed
from messunjerr.profiles.services import create_profile_services
from messunjerr.settings import Settings

ADMIN_ROLE = "admin"


class AdminError(Exception):
    """Команду нельзя выполнить; сообщение написано для оператора."""


class PasswordRequiredError(AdminError):
    """Аккаунта с такой почтой нет, а для нового нужен пароль."""


@dataclass(frozen=True, slots=True)
class AdminResult:
    user_id: uuid.UUID
    username: str
    created: bool
    """`True`: аккаунт создан; `False`: роль выдана существующему."""
    ignored: tuple[str, ...] = ()
    """Что оператор передал, а команда не применила: у существующего аккаунта пароль и ник не
    меняются (роль выдаётся по почте). Оператор, ждавший смену пароля, увидит это в предупреждении."""


async def _find(session: AsyncSession, column: str, value: str) -> UserRow | None:
    field = UserRow.email if column == "email" else UserRow.username
    return (
        await session.execute(select(UserRow).where(field == value).with_for_update())
    ).scalar_one_or_none()


async def create_admin(
    settings: Settings, *, email: str, username: str, password: str | None
) -> AdminResult:
    """Создаёт администратора или повышает существующий аккаунт с этой почтой."""
    try:
        address = normalize_email(email)
    except InvalidEmailError as error:
        raise AdminError(f"адрес почты не годится ({error.problem.value})") from error
    name = normalize_username(username)
    if not USERNAME_PATTERN.fullmatch(name):
        raise AdminError("ник: 3–30 символов a–z, 0–9 и _")

    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session, session.begin():
            by_email = await _find(session, "email", address)
            by_name = await _find(session, "username", name)
            if by_email is not None:
                if by_name is not None and by_name.id != by_email.id:
                    raise AdminError(f"ник {name} занят другим аккаунтом")
                if by_email.status != "active":
                    raise AdminError(
                        f"аккаунт {by_email.username} в статусе {by_email.status}: роль выдают только активным"
                    )
                previous = by_email.role
                by_email.role = ADMIN_ROLE
                record_audit(
                    session,
                    action=audit.ROLE_CHANGED,
                    actor_id=None,
                    target_type=audit.TARGET_USER,
                    target_id=by_email.id,
                    data={"from": previous, "to": ADMIN_ROLE, "via": "cli"},
                )
                # Пароль не хэшируется (Argon2 стоит сотни миллисекунд) и не применяется: о том, что
                # пароль и ник остались прежними, скажет предупреждение команды.
                ignored = tuple(
                    item
                    for item, given in (
                        ("пароль", password is not None),
                        ("ник", by_email.username != name),
                    )
                    if given
                )
                return AdminResult(by_email.id, by_email.username, created=False, ignored=ignored)
            if by_name is not None:
                raise AdminError(f"ник {name} занят другим аккаунтом (с другой почтой)")
            if password is None:
                raise PasswordRequiredError("аккаунта с такой почтой нет: для нового нужен пароль")
            problem = check_password_policy(password, username=name, email=address)
            if problem is not None:
                raise AdminError(f"пароль не годится ({problem.value})")
            passwords = PasswordService.from_settings(settings)
            try:
                password_hash = await passwords.hash(password)
            finally:
                passwords.shutdown()

            now = datetime.now(UTC)
            user = UserRow(
                email=address,
                email_verified_at=now,
                username=name,
                terms_version=settings.legal_terms_version,
                terms_accepted_at=now,
                password_hash=password_hash,
                role=ADMIN_ROLE,
                status="active",
            )
            session.add(user)
            await session.flush()
            await create_profile_services().provision(
                session, user_id=user.id, username=name, seed=ProfileSeed()
            )
            record_audit(
                session,
                action=audit.ROLE_CHANGED,
                actor_id=None,
                target_type=audit.TARGET_USER,
                target_id=user.id,
                data={"from": None, "to": ADMIN_ROLE, "via": "cli", "created": True},
            )
            return AdminResult(user.id, name, created=True)
    finally:
        await engine.dispose()

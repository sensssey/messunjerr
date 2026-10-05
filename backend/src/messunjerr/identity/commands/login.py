"""Команда `POST /auth/login` (5.2): проверка пароля и статуса, выдача сессии."""

from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.clock import utcnow
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    SignedIn,
    ensure_can_sign_in,
    start_session,
)
from messunjerr.identity.domain.errors import invalid_credentials
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.repositories import UserRepository
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class Login:
    login: str
    """Почта или ник."""
    password: str
    client: ClientInfo


async def login(
    command: Login,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    tokens: TokenService,
    settings: Settings,
    now: datetime | None = None,
) -> SignedIn:
    """Порядок важен: статус аккаунта сообщается только после верного пароля.

    Для несуществующего логина и для аккаунта без пароля тратится столько же времени, сколько на
    проверку настоящего пароля, а ответ одинаков (`invalid_credentials`).
    """
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_login(command.login)
    if user is None or user.password_hash is None:
        await passwords.burn(command.password)
        raise invalid_credentials()

    check = await passwords.verify(command.password, user.password_hash)
    if not check.valid:
        raise invalid_credentials()
    ensure_can_sign_in(user)

    if check.new_hash is not None:
        user.password_hash = check.new_hash  # параметры Argon2id изменились: обновляем хэш
    grant = start_session(
        session=uow.session,
        tokens=tokens,
        settings=settings,
        user=user,
        client=command.client,
        now=moment,
    )
    user.last_login_at = moment
    await uow.commit()
    return SignedIn(user=MeUser.from_row(user), grant=grant)

"""Команда `POST /auth/verify-email` (5.2): подтверждение адреса и автоматический вход."""

from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.clock import utcnow
from messunjerr.core.security import hash_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    SignedIn,
    ensure_can_sign_in,
    start_session,
)
from messunjerr.identity.commands.mail import PURPOSE_VERIFY_EMAIL
from messunjerr.identity.domain.errors import token_invalid_or_expired
from messunjerr.identity.domain.events import EmailVerified, record
from messunjerr.identity.infra.jwt_service import TokenService
from messunjerr.identity.infra.repositories import EmailTokenRepository, UserRepository
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class VerifyEmail:
    token: str
    client: ClientInfo


async def verify_email(
    command: VerifyEmail,
    *,
    uow: UnitOfWork,
    tokens: TokenService,
    settings: Settings,
    now: datetime | None = None,
) -> SignedIn:
    """Гасит токен, активирует аккаунт и выдаёт сессию.

    Токен одноразовый: строка блокируется, поэтому два одновременных запроса с одним токеном не
    пройдут оба. Если вход невозможен (аккаунт заблокирован), транзакция откатывается и токен
    остаётся непогашенным.
    """
    moment = now or utcnow()
    token_row = await EmailTokenRepository(uow.session).get_active_for_update(
        hash_token(command.token), PURPOSE_VERIFY_EMAIL, moment
    )
    if token_row is None:
        raise token_invalid_or_expired()
    user = await UserRepository(uow.session).get_by_id(token_row.user_id, for_update=True)
    if user is None:
        raise token_invalid_or_expired()

    token_row.consumed_at = moment
    if user.status == "pending":
        user.status = "active"
    if user.email_verified_at is None:
        user.email_verified_at = moment
        record(uow.outbox, EmailVerified(user.id))
    ensure_can_sign_in(user)

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

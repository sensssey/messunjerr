"""Смена почты (5.2): `POST /auth/email/change` и `POST /auth/email/confirm`.

Запрос (с паролем) шлёт токен на **новый** адрес, а старому отправляет уведомление: если смену
затеял не владелец, он узнает об этом. Адрес заменяется только после подтверждения токеном.

Запрос на адрес, который уже занят другим аккаунтом, отвечает так же, как успешный (`202`), но письма
не отправляет: по ответу занятость адреса не проверить.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode
from messunjerr.core.jobs import JobQueue
from messunjerr.core.ratelimit import RateLimiter
from messunjerr.core.security import hash_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import ClientInfo, verify_reauth
from messunjerr.identity.commands.logout import Actor
from messunjerr.identity.commands.mail import (
    PURPOSE_CHANGE_EMAIL,
    PURPOSE_RESET_PASSWORD,
    PURPOSE_VERIFY_EMAIL,
    issue_email_change_emails,
    send_email_changed_notice,
)
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import token_invalid_or_expired, unauthorized
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.repositories import (
    EmailTokenRepository,
    UserRepository,
    violated_constraint,
)
from messunjerr.settings import Settings


@dataclass(frozen=True, slots=True)
class RequestEmailChange:
    actor: Actor
    new_email: str
    password: str
    client: ClientInfo


async def request_email_change(
    command: RequestEmailChange,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    limiter: RateLimiter,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    moment = now or utcnow()
    users = UserRepository(uow.session)
    user = await users.get_by_id(command.actor.user_id, for_update=True)
    if user is None:
        raise unauthorized(ErrorCode.TOKEN_INVALID, "The user of this token no longer exists.")
    await verify_reauth(
        uow=uow,
        user=user,
        password=command.password,
        passwords=passwords,
        limiter=limiter,
        client=command.client,
    )

    if command.new_email != user.email and await users.get_by_email(command.new_email) is None:
        await issue_email_change_emails(
            session=uow.session,
            jobs=jobs,
            settings=settings,
            user=user,
            new_email=command.new_email,
            now=moment,
        )
        record_audit(
            uow.session,
            action=audit.EMAIL_CHANGE_REQUESTED,
            actor_id=user.id,
            target_type=audit.TARGET_USER,
            target_id=user.id,
            ip=command.client.ip,
            user_agent=command.client.user_agent,
        )
    await uow.commit()


async def confirm_email_change(
    token: str,
    *,
    uow: UnitOfWork,
    jobs: JobQueue,
    settings: Settings,
    client: ClientInfo,
    now: datetime | None = None,
) -> None:
    """Заменяет адрес по токену из письма. Сессии остаются: смена почты их не отзывает."""
    moment = now or utcnow()
    token_row = await EmailTokenRepository(uow.session).get_active_for_update(
        hash_token(token), PURPOSE_CHANGE_EMAIL, moment
    )
    if token_row is None or token_row.new_email is None:
        raise token_invalid_or_expired()
    users = UserRepository(uow.session)
    user = await users.get_by_id(token_row.user_id, for_update=True)
    if user is None:
        raise token_invalid_or_expired()
    new_email = token_row.new_email
    holder = await users.get_by_email(new_email)
    if holder is not None and holder.id != user.id:
        raise token_invalid_or_expired()  # адрес успели занять, пока токен лежал в письме

    old_email = user.email
    try:
        async with uow.session.begin_nested():
            user.email = new_email
            user.email_verified_at = moment  # токен пришёл на новый адрес
            token_row.consumed_at = moment
            await uow.session.flush()
    except IntegrityError as error:
        if violated_constraint(error) == "uq_users_email":
            raise token_invalid_or_expired() from error
        raise
    # Токены, выданные на прежний адрес (сброс пароля и прочие), после смены почты больше не действуют:
    # иначе тот, кто читал старый ящик, ещё час мог бы сбросить пароль.
    for purpose in (PURPOSE_RESET_PASSWORD, PURPOSE_VERIFY_EMAIL, PURPOSE_CHANGE_EMAIL):
        await EmailTokenRepository(uow.session).invalidate_active(user.id, purpose, moment)
    record_audit(
        uow.session,
        action=audit.EMAIL_CHANGED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    uow.after_commit(
        lambda: send_email_changed_notice(
            jobs=jobs, settings=settings, old_email=old_email, new_email=new_email
        )
    )
    await uow.commit()

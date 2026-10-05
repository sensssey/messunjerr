"""Удаление аккаунта и его восстановление (5.3, S3-06): `DELETE /me` и `POST /me/restore`.

`DELETE /me` не стирает данные, а ставит срок: статус `deletion_pending`, `deletion_scheduled_at`
через `ACCOUNT_DELETION_GRACE_DAYS` дней, все сессии кроме текущей закрыты. Профиль и контент
перестают быть видны другим (аккаунт не `active`). Дальше человек может восстановить аккаунт до срока;
иначе данные уничтожит плановая задача S18 по `deletion_scheduled_at`.

Пока аккаунт ждёт удаления, пускают только `GET /me`, `POST /me/restore` и выход (5.1). Эту границу
держит признак в Redis (`acct:deletion:{user_id}`): статус из БД на каждый запрос не читается (4.7).

Подтверждение: пароль. Аккаунту без пароля (только вход через OAuth, S21) достаточно свежей сессии,
не старше пяти минут (5.3).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ItemCode
from messunjerr.core.jobs import JobQueue
from messunjerr.core.ratelimit import RateLimiter
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    confirm_deletion_flag_after_commit,
    revoke_in_denylist_after_commit,
    verify_reauth,
)
from messunjerr.identity.commands.logout import Actor
from messunjerr.identity.commands.mail import send_deletion_requested_notice
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import (
    field_error,
    not_pending_deletion,
    reauth_failed,
    role_must_be_revoked,
    token_user_gone,
)
from messunjerr.identity.domain.events import UserDeletionRequested, record
from messunjerr.identity.infra.models import UserRow
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.ports import MeExtrasProvider
from messunjerr.identity.infra.repositories import SessionRepository, UserRepository
from messunjerr.identity.infra.session_denylist import SessionDenylist
from messunjerr.identity.queries.me import build_me
from messunjerr.identity.queries.models import MeUser
from messunjerr.settings import Settings

FRESH_SESSION = timedelta(minutes=5)
"""Насколько свежей должна быть сессия аккаунта без пароля, чтобы подтвердить удаление."""
PRIVILEGED_ROLES = frozenset({"moderator", "admin"})


@dataclass(frozen=True, slots=True)
class RequestDeletion:
    actor: Actor
    password: str | None
    """`None`, если в теле запроса пароля нет: это допустимо только аккаунту без пароля."""
    client: ClientInfo


async def _confirm_identity(
    command: RequestDeletion,
    *,
    uow: UnitOfWork,
    user: UserRow,
    passwords: PasswordService,
    limiter: RateLimiter,
    now: datetime,
) -> None:
    if user.password_hash is not None:
        if command.password is None:
            raise field_error(
                "/body/password", ItemCode.REQUIRED, "The current password is required."
            )
        await verify_reauth(
            uow=uow,
            user=user,
            password=command.password,
            passwords=passwords,
            limiter=limiter,
            client=command.client,
        )
        return
    session = await SessionRepository(uow.session).get(command.actor.session_id)
    if session is None or now - session.created_at > FRESH_SESSION:
        raise reauth_failed()


async def request_deletion(
    command: RequestDeletion,
    *,
    uow: UnitOfWork,
    passwords: PasswordService,
    limiter: RateLimiter,
    denylist: SessionDenylist,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> datetime:
    """Ставит аккаунт в очередь на удаление и возвращает срок (`deletion_scheduled_at`).

    Повторный вызов (две вкладки нажали одновременно) срок не сдвигает: возвращается прежний.
    """
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_id(command.actor.user_id, for_update=True)
    if user is None:
        raise token_user_gone()
    if user.status == "deletion_pending" and user.deletion_scheduled_at is not None:
        # Повторный запрос (вторая вкладка или признак, потерянный вместе с Redis): срок прежний, а
        # признак ограничения подтверждается заново.
        confirm_deletion_flag_after_commit(uow, denylist, user)
        await uow.commit()
        return user.deletion_scheduled_at

    await _confirm_identity(
        command, uow=uow, user=user, passwords=passwords, limiter=limiter, now=moment
    )
    if user.role in PRIVILEGED_ROLES:
        raise role_must_be_revoked()

    scheduled_at = moment + timedelta(days=settings.account_deletion_grace_days)
    user.status = "deletion_pending"
    user.deletion_scheduled_at = scheduled_at
    revoked = await SessionRepository(uow.session).revoke_all(
        user.id, reason="deletion_requested", now=moment, keep=command.actor.session_id
    )
    record(uow.outbox, UserDeletionRequested(user.id))
    record_audit(
        uow.session,
        action=audit.ACCOUNT_DELETION_REQUESTED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
        data={"revoked_sessions": len(revoked), "grace_days": settings.account_deletion_grace_days},
    )
    revoke_in_denylist_after_commit(uow, denylist, revoked)
    # Текущая сессия остаётся, но с этой минуты ей разрешены только GET /me, restore и выход.
    confirm_deletion_flag_after_commit(uow, denylist, user)
    uow.after_commit(
        lambda: send_deletion_requested_notice(
            jobs=jobs, settings=settings, user=user, scheduled_at=scheduled_at
        )
    )
    await uow.commit()
    return scheduled_at


async def restore_account(
    *,
    user_id: uuid.UUID,
    uow: UnitOfWork,
    denylist: SessionDenylist,
    me_extras: MeExtrasProvider,
    client: ClientInfo,
    now: datetime | None = None,
) -> MeUser:
    """Отменяет удаление до наступления срока и возвращает `MeUser`.

    После срока восстановить нельзя, даже если задача уничтожения ещё не добралась до аккаунта:
    `409 not_pending_deletion`. Закрытые при запросе сессии остаются закрытыми.
    """
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_id(user_id, for_update=True)
    if user is None:
        raise token_user_gone()
    if user.status != "deletion_pending":
        raise not_pending_deletion()
    if user.deletion_scheduled_at is not None and moment >= user.deletion_scheduled_at:
        raise not_pending_deletion(
            "The grace period has elapsed; the account can no longer be restored."
        )

    user.status = "active"
    user.deletion_scheduled_at = None
    record_audit(
        uow.session,
        action=audit.ACCOUNT_RESTORED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    uow.after_commit(lambda: denylist.clear_deletion_pending(user.id))
    me = await build_me(uow.session, user, me_extras)
    await uow.commit()
    return me

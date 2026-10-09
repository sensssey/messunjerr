"""Выход и управление сессиями (5.2): `logout`, `logout-all`, `DELETE /auth/sessions/{id}`.

Отзыв всегда двойной: `revoked_at` в таблице сессий (источник истины, refresh перестаёт работать
сразу) и ключ в denylist Redis после коммита (уже выданные access-токены перестают приниматься сразу,
а не через 20 минут).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.ratelimit import RateLimiter
from messunjerr.core.security import hash_token
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import (
    ClientInfo,
    revoke_in_denylist_after_commit,
    verify_reauth,
)
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import not_found, token_user_gone
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.repositories import SessionRepository, UserRepository
from messunjerr.identity.infra.session_denylist import SessionDenylist


@dataclass(frozen=True, slots=True)
class Actor:
    """Кто выполняет действие: данные из проверенного access-токена."""

    user_id: uuid.UUID
    session_id: uuid.UUID


async def logout(
    refresh_token: str | None,
    *,
    uow: UnitOfWork,
    denylist: SessionDenylist,
    client: ClientInfo,
    now: datetime | None = None,
) -> None:
    """Отзывает сессию по её refresh-токену (текущему или предыдущему). Идемпотентна: нет токена,
    сессия не найдена или уже закрыта означает «уже вышли», без ошибки."""
    if not refresh_token:
        return
    moment = now or utcnow()
    digest = hash_token(refresh_token)
    sessions = SessionRepository(uow.session)
    session = await sessions.get_by_refresh_hash(
        digest, for_update=True
    ) or await sessions.get_by_prev_refresh_hash(digest, for_update=True)
    if session is None or session.revoked_at is not None:
        return
    session.revoked_at = moment
    session.revoked_reason = "logout"
    record_audit(
        uow.session,
        action=audit.LOGOUT,
        actor_id=session.user_id,
        target_type=audit.TARGET_SESSION,
        target_id=session.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    revoke_in_denylist_after_commit(uow, denylist, [session.id])
    await uow.commit()


async def logout_all(
    *,
    actor: Actor,
    password: str,
    keep_current: bool,
    uow: UnitOfWork,
    passwords: PasswordService,
    limiter: RateLimiter,
    denylist: SessionDenylist,
    client: ClientInfo,
    now: datetime | None = None,
) -> None:
    """«Выйти везде»: требует пароль; отзывает все сессии, кроме текущей при `keep_current`."""
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_id(actor.user_id, for_update=True)
    if user is None:
        raise token_user_gone()
    await verify_reauth(
        uow=uow, user=user, password=password, passwords=passwords, limiter=limiter, client=client
    )
    revoked = await SessionRepository(uow.session).revoke_all(
        user.id,
        reason="logout_all",
        now=moment,
        keep=actor.session_id if keep_current else None,
    )
    record_audit(
        uow.session,
        action=audit.LOGOUT_ALL,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=client.ip,
        user_agent=client.user_agent,
        data={"revoked": len(revoked), "keep_current": keep_current},
    )
    revoke_in_denylist_after_commit(uow, denylist, revoked)
    await uow.commit()


async def revoke_session(
    *,
    actor: Actor,
    session_id: uuid.UUID,
    uow: UnitOfWork,
    denylist: SessionDenylist,
    client: ClientInfo,
    now: datetime | None = None,
) -> bool:
    """Отзывает одну из своих сессий; `True`, если это была текущая (клиент сотрёт cookie).

    Чужая, несуществующая и уже закрытая сессии одинаково дают `404`: чужие идентификаторы не
    проверить перебором.
    """
    moment = now or utcnow()
    session = await SessionRepository(uow.session).get(session_id, for_update=True)
    if session is None or session.user_id != actor.user_id or session.revoked_at is not None:
        raise not_found()
    session.revoked_at = moment
    session.revoked_reason = "logout"
    record_audit(
        uow.session,
        action=audit.SESSION_REVOKED,
        actor_id=actor.user_id,
        target_type=audit.TARGET_SESSION,
        target_id=session.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    revoke_in_denylist_after_commit(uow, denylist, [session.id])
    await uow.commit()
    return session.id == actor.session_id

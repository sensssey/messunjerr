"""Письма контекста identity: токен подтверждения и постановка задачи `send_email`.

Письмо отправляет воркер (задача `send_email`), команда только ставит её в очередь. Если очередь
недоступна, адаптер бросает `503`, транзакция откатывается, и человек повторяет запрос: так не
бывает «аккаунт создан, а письма нет». Обратное возможно: письмо со ссылкой на токен, который не
записался из-за отката; такая ссылка просто недействительна.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.ids import uuid7
from messunjerr.core.jobs import QUEUE_EMAIL, TASK_SEND_EMAIL, JobQueue
from messunjerr.core.security import hash_token, new_opaque_token
from messunjerr.identity.domain.emails import mask_email
from messunjerr.identity.infra.models import EmailTokenRow, UserRow
from messunjerr.identity.infra.repositories import EmailTokenRepository
from messunjerr.settings import Settings

PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET_PASSWORD = "reset_password"  # noqa: S105 (назначение токена, не пароль)
PURPOSE_CHANGE_EMAIL = "change_email"


async def enqueue_email(
    jobs: JobQueue,
    *,
    to: str,
    template: str,
    context: Mapping[str, Any],
    job_id: str | None = None,
) -> None:
    await jobs.enqueue(
        TASK_SEND_EMAIL,
        queue=QUEUE_EMAIL,
        job_id=job_id,
        to=to,
        template=template,
        context=dict(context),
    )


async def issue_verification_email(
    *, session: AsyncSession, jobs: JobQueue, settings: Settings, user: UserRow, now: datetime
) -> None:
    """Выдаёт новый токен подтверждения (прежние гасит) и ставит письмо в очередь.

    Ссылка ведёт на страницу клиента, токен лежит во фрагменте (`#token=…`): фрагмент не уходит
    на сервер, поэтому не попадает ни в журналы Caddy, ни в заголовок `Referer`.
    """
    token, row = await _issue_token(
        session,
        user=user,
        purpose=PURPOSE_VERIFY_EMAIL,
        ttl=timedelta(hours=settings.email_verification_ttl_hours),
        now=now,
    )
    await enqueue_email(
        jobs,
        to=user.email,
        template="verify_email",
        context={
            "verify_url": f"{settings.base_url}/verify-email#token={token}",
            "token": token,
            "ttl_hours": settings.email_verification_ttl_hours,
        },
        job_id=f"verify_email:{row.id}",
    )


async def send_account_exists_email(*, jobs: JobQueue, settings: Settings, user: UserRow) -> None:
    await enqueue_email(
        jobs,
        to=user.email,
        template="account_exists",
        context={
            "login_url": f"{settings.base_url}/login",
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )


async def _issue_token(
    session: AsyncSession,
    *,
    user: UserRow,
    purpose: str,
    ttl: timedelta,
    now: datetime,
    new_email: str | None = None,
) -> tuple[str, EmailTokenRow]:
    """Новый токен для письма (прежние того же назначения гасит); хранится только его SHA-256."""
    tokens = EmailTokenRepository(session)
    await tokens.invalidate_active(user.id, purpose, now)
    token = new_opaque_token()
    row = EmailTokenRow(
        id=uuid7(),
        user_id=user.id,
        purpose=purpose,
        token_hash=hash_token(token),
        new_email=new_email,
        expires_at=now + ttl,
    )
    tokens.add(row)
    return token, row


async def issue_password_reset_email(
    *, session: AsyncSession, jobs: JobQueue, settings: Settings, user: UserRow, now: datetime
) -> None:
    """Письмо со ссылкой для сброса пароля: токен действует один час и один раз."""
    ttl = timedelta(minutes=settings.password_reset_ttl_minutes)
    token, row = await _issue_token(
        session, user=user, purpose=PURPOSE_RESET_PASSWORD, ttl=ttl, now=now
    )
    await enqueue_email(
        jobs,
        to=user.email,
        template="reset_password",
        context={
            "reset_url": f"{settings.base_url}/reset-password#token={token}",
            "token": token,
            "ttl_minutes": settings.password_reset_ttl_minutes,
        },
        job_id=f"reset_password:{row.id}",
    )


async def issue_email_change_emails(
    *,
    session: AsyncSession,
    jobs: JobQueue,
    settings: Settings,
    user: UserRow,
    new_email: str,
    now: datetime,
) -> None:
    """Письмо с подтверждением на новый адрес и уведомление на старый (4.7, «Письменные токены»)."""
    ttl = timedelta(minutes=settings.email_change_ttl_minutes)
    token, row = await _issue_token(
        session, user=user, purpose=PURPOSE_CHANGE_EMAIL, ttl=ttl, now=now, new_email=new_email
    )
    await enqueue_email(
        jobs,
        to=new_email,
        template="email_change_confirm",
        context={
            "confirm_url": f"{settings.base_url}/confirm-email#token={token}",
            "token": token,
            "ttl_minutes": settings.email_change_ttl_minutes,
        },
        job_id=f"change_email:{row.id}",
    )
    await enqueue_email(
        jobs,
        to=user.email,
        template="email_change_notice",
        context={
            "new_email_masked": mask_email(new_email),
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )


async def send_password_changed_notice(
    *, jobs: JobQueue, settings: Settings, user: UserRow, now: datetime
) -> None:
    """Уведомление о смене или сбросе пароля. Лучшее усилие: вызывается после коммита."""
    await enqueue_email(
        jobs,
        to=user.email,
        template="password_changed",
        context={
            "when": now.strftime("%d.%m.%Y %H:%M UTC"),
            "login_url": f"{settings.base_url}/login",
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )


async def send_deletion_requested_notice(
    *, jobs: JobQueue, settings: Settings, user: UserRow, scheduled_at: datetime
) -> None:
    """Письмо о запросе удаления: когда данные будут стёрты и как передумать. Лучшее усилие, после коммита."""
    await enqueue_email(
        jobs,
        to=user.email,
        template="account_deletion_requested",
        context={
            "scheduled_at": scheduled_at.strftime("%d.%m.%Y %H:%M UTC"),
            "grace_days": settings.account_deletion_grace_days,
            "login_url": f"{settings.base_url}/login",
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )


async def send_refresh_reuse_notice(
    *, jobs: JobQueue, settings: Settings, user: UserRow, device: str | None
) -> None:
    """Письмо о повторном использовании refresh-токена: сессия закрыта, советуем сменить пароль."""
    await enqueue_email(
        jobs,
        to=user.email,
        template="refresh_reuse",
        context={
            "device": device,
            "login_url": f"{settings.base_url}/login",
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )


async def send_email_changed_notice(
    *, jobs: JobQueue, settings: Settings, old_email: str, new_email: str
) -> None:
    """Письмо на прежний адрес: почта аккаунта заменена."""
    await enqueue_email(
        jobs,
        to=old_email,
        template="email_changed",
        context={
            "new_email_masked": mask_email(new_email),
            "reset_url": f"{settings.base_url}/forgot-password",
        },
    )

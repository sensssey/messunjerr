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
from messunjerr.identity.infra.models import EmailTokenRow, UserRow
from messunjerr.identity.infra.repositories import EmailTokenRepository
from messunjerr.settings import Settings

PURPOSE_VERIFY_EMAIL = "verify_email"


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
    tokens = EmailTokenRepository(session)
    await tokens.invalidate_active(user.id, PURPOSE_VERIFY_EMAIL, now)
    token = new_opaque_token()
    row = EmailTokenRow(
        id=uuid7(),
        user_id=user.id,
        purpose=PURPOSE_VERIFY_EMAIL,
        token_hash=hash_token(token),
        expires_at=now + timedelta(hours=settings.email_verification_ttl_hours),
    )
    tokens.add(row)
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

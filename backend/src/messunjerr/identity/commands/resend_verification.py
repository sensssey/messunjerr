"""Команда `POST /auth/resend-verification` (5.2): повторное письмо с токеном подтверждения.

Ответ `202` не зависит от того, есть ли адрес и подтверждён ли он. Чтобы не выдавать это временем,
вся работа выполняется после отправки ответа (фоновой задачей запроса).
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.mail import issue_verification_email
from messunjerr.identity.infra.repositories import UserRepository
from messunjerr.settings import Settings


async def resend_verification(
    email: str,
    *,
    uow: UnitOfWork,
    jobs: JobQueue,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Выдаёт новый токен неподтверждённому аккаунту; `True`, если письмо поставлено в очередь."""
    moment = now or utcnow()
    user = await UserRepository(uow.session).get_by_email(email, for_update=True)
    if user is None or user.status != "pending":
        return False
    await issue_verification_email(
        session=uow.session, jobs=jobs, settings=settings, user=user, now=moment
    )
    await uow.commit()
    return True


async def resend_verification_after_response(
    email: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    settings: Settings,
) -> None:
    """Обёртка для фоновой задачи запроса: ответ уже отправлен, поэтому ошибки только в журнал.

    В журнал идёт лишь тип исключения: текст ошибки БД содержит параметры запроса, то есть адрес.
    """
    try:
        async with UnitOfWork(sessionmaker) as uow:
            await resend_verification(email, uow=uow, jobs=jobs, settings=settings)
    except Exception as error:
        get_logger("messunjerr.identity").error(
            "resend_verification_failed", error_type=type(error).__name__
        )

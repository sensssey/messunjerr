"""Плановая очистка identity: неподтверждённые аккаунты, отработавшие токены из писем, резервы ников.

Запускает воркер очереди `default` (cron, `messunjerr.jobs`). Все операции безопасны при повторе и
при параллельном запуске двух воркеров: удаляют пачками с `FOR UPDATE SKIP LOCKED`.
"""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.domain.audit import ACCOUNT_UNVERIFIED_PURGED, TARGET_USER
from messunjerr.identity.infra.repositories import (
    EmailTokenRepository,
    UsernameReservationRepository,
    UserRepository,
)

BATCH_SIZE = 500
SPENT_TOKEN_GRACE = timedelta(days=7)
"""Срок хранения одноразовых токенов: до срока действия и ещё 7 дней (спецификация 6.9.7). Так проще
разбирать жалобы «ссылка не работает»; для самого API разницы нет, неверный и просроченный токены
отвечают одинаково."""


async def purge_unverified_accounts(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    older_than: timedelta,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Удаляет неподтверждённые аккаунты старше `older_than`; в аудит пишется по записи на аккаунт.

    Такой аккаунт занимает ник и почту, хотя владелец так и не открыл письмо. Возвращает число
    удалённых аккаунтов. Каждая пачка фиксируется отдельной транзакцией.
    """
    moment = now or utcnow()
    removed = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            ids = await UserRepository(uow.session).delete_stale_pending(
                cutoff=moment - older_than, now=moment, limit=batch_size
            )
            for user_id in ids:
                record_audit(
                    uow.session,
                    action=ACCOUNT_UNVERIFIED_PURGED,
                    actor_id=None,
                    target_type=TARGET_USER,
                    target_id=user_id,
                    data={"older_than_days": older_than.days},
                )
            await uow.commit()
        removed += len(ids)
        if len(ids) < batch_size:
            return removed


async def purge_username_reservations(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Удаляет резервы прежних ников, срок которых вышел (S3-03).

    Для самого API резерв с истёкшим сроком уже не существует (он не учитывается при проверках), так
    что очистка нужна только чтобы таблица не росла.
    """
    moment = now or utcnow()
    removed = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            count = await UsernameReservationRepository(uow.session).delete_expired(
                now=moment, limit=batch_size
            )
            await uow.commit()
        removed += count
        if count < batch_size:
            return removed


async def purge_spent_tokens(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    grace: timedelta = SPENT_TOKEN_GRACE,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Удаляет токены из писем, просроченные или использованные дольше `grace` назад."""
    cutoff = (now or utcnow()) - grace
    removed = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            count = await EmailTokenRepository(uow.session).delete_spent(
                cutoff=cutoff, limit=batch_size
            )
            await uow.commit()
        removed += count
        if count < batch_size:
            return removed

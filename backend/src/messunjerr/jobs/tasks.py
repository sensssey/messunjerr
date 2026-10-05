"""Фоновые задачи. Каждая идемпотентна или безопасна при повторе (at-least-once, 4.12).

Первый аргумент задачи arq: `ctx` с ресурсами воркера (см. `messunjerr.jobs.worker`).
"""

from datetime import timedelta
from typing import Any, cast

from arq.worker import Retry
from jinja2 import TemplateError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.idempotency import purge_expired_keys
from messunjerr.core.logs import get_logger
from messunjerr.core.mail import Mailer, PermanentMailError, TransientMailError, render_email
from messunjerr.identity.commands.housekeeping import purge_spent_tokens, purge_unverified_accounts
from messunjerr.settings import Settings

SEND_EMAIL_MAX_TRIES = 5
SEND_EMAIL_RETRY_BASE_SECONDS = 30
"""Повторы отправки идут с экспонентой от 30 с: 30, 60, 120, 240 секунд (спецификация 4.12)."""


async def send_email(
    ctx: dict[str, Any], *, to: str, template: str, context: dict[str, Any]
) -> None:
    """Собирает письмо из шаблона и отправляет его через почтовый порт воркера.

    В журнал не пишутся ни адрес получателя, ни содержимое письма: в нём токены и персональные
    данные. Ошибка шаблона и отказ сервера (5xx) повторять бессмысленно; временный сбой
    повторяется, а после последней попытки задача считается проваленной.
    """
    log = get_logger("messunjerr.jobs.email")
    settings = cast(Settings, ctx["settings"])
    mailer = cast(Mailer, ctx["mailer"])
    attempt = int(ctx.get("job_try", 1))

    try:
        message = render_email(template, context, sender=settings.mail_from, to=to)
    except (TemplateError, ValueError, KeyError) as error:
        log.error("send_email_render_failed", template=template, error_type=type(error).__name__)
        return

    try:
        await mailer.send(message)
    except PermanentMailError as error:
        log.error("send_email_rejected", template=template, reason=str(error))
        return
    except TransientMailError as error:
        if attempt >= SEND_EMAIL_MAX_TRIES:
            log.error("send_email_gave_up", template=template, attempts=attempt)
            raise
        delay = SEND_EMAIL_RETRY_BASE_SECONDS * 2 ** (attempt - 1)
        log.warning("send_email_retry", template=template, attempt=attempt, retry_in=delay)
        raise Retry(defer=delay) from error
    log.info("send_email_sent", template=template)


async def cleanup_unverified_accounts(ctx: dict[str, Any]) -> int:
    """Удаляет аккаунты, которые не подтвердили почту за `UNVERIFIED_ACCOUNT_TTL_DAYS` дней.

    Иначе они занимают ник и почту навсегда. Повтор и параллельный запуск безопасны.
    """
    settings = cast(Settings, ctx["settings"])
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    removed = await purge_unverified_accounts(
        sessionmaker, older_than=timedelta(days=settings.unverified_account_ttl_days)
    )
    get_logger("messunjerr.jobs.cleanup").info("cleanup_unverified_accounts", removed=removed)
    return removed


async def cleanup_tokens_and_idempotency(ctx: dict[str, Any]) -> dict[str, int]:
    """Удаляет токены из писем и записи `Idempotency-Key`, срок которых вышел."""
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    removed = {
        "email_tokens": await purge_spent_tokens(sessionmaker),
        "idempotency_keys": await purge_expired_keys(sessionmaker),
    }
    # Ключ `email_tokens` фильтр журнала принял бы за секрет и скрыл: пишем под другим именем.
    get_logger("messunjerr.jobs.cleanup").info(
        "cleanup_tokens_and_idempotency",
        mail_links=removed["email_tokens"],
        idempotency_keys=removed["idempotency_keys"],
    )
    return removed

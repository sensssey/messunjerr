"""Фоновые задачи. Каждая идемпотентна или безопасна при повторе (at-least-once, 4.12).

Первый аргумент задачи arq: `ctx` с ресурсами воркера (см. `messunjerr.jobs.worker`).
"""

import uuid
from datetime import timedelta
from typing import Any, cast

from arq.worker import Retry
from jinja2 import TemplateError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.idempotency import purge_expired_keys
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.mail import Mailer, PermanentMailError, TransientMailError, render_email
from messunjerr.identity.commands.housekeeping import (
    purge_spent_tokens,
    purge_unverified_accounts,
    purge_username_reservations,
)
from messunjerr.media.commands import housekeeping as media_housekeeping
from messunjerr.media.commands.process_media import ProcessMedia
from messunjerr.media.commands.process_media import process_media as run_process_media
from messunjerr.media.domain.ports import ObjectStorage, StorageUnavailableError
from messunjerr.media.infra.images import DecodeBudget
from messunjerr.settings import Settings

SEND_EMAIL_MAX_TRIES = 5
SEND_EMAIL_RETRY_BASE_SECONDS = 30
"""Повторы отправки идут с экспонентой от 30 с: 30, 60, 120, 240 секунд (спецификация 4.12)."""

PROCESS_MEDIA_MAX_TRIES = 3
PROCESS_MEDIA_RETRY_BASE_SECONDS = 30
PROCESS_MEDIA_TIMEOUT_SECONDS = 120
DELETE_MEDIA_OBJECTS_MAX_TRIES = 5
DELETE_MEDIA_OBJECTS_TIMEOUT_SECONDS = 120
DELETE_MEDIA_OBJECTS_RETRY_BASE_SECONDS = 30


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


async def process_media(ctx: dict[str, Any], *, asset_id: str) -> str:
    """Обработка загруженного файла: тип по содержимому, варианты изображений в WebP без EXIF.

    Сбой хранилища повторяется с нарастающей паузой. После последней попытки задача сдаётся, а ресурс
    остаётся в `processing`: недоступное хранилище не повод отклонять и стирать файлы людей, их повторно
    поставит `reconcile_uploads`, а через сутки их удалит очистка. Журнал не содержит имени файла.
    """
    log = get_logger("messunjerr.jobs.media")
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    storage = cast(ObjectStorage, ctx["storage"])
    jobs = cast(JobQueue, ctx["jobs"])
    budget = cast("DecodeBudget | None", ctx.get("decode_budget"))
    attempt = int(ctx.get("job_try", 1))
    parsed = uuid.UUID(asset_id)
    try:
        outcome = await run_process_media(
            ProcessMedia(parsed),
            sessionmaker=sessionmaker,
            storage=storage,
            jobs=jobs,
            budget=budget,
        )
    except StorageUnavailableError as error:
        if attempt >= PROCESS_MEDIA_MAX_TRIES:
            log.error("process_media_gave_up", asset_id=asset_id, attempts=attempt)
            return "deferred"
        delay = PROCESS_MEDIA_RETRY_BASE_SECONDS * attempt
        log.warning("process_media_retry", asset_id=asset_id, attempt=attempt, retry_in=delay)
        raise Retry(defer=delay) from error
    return outcome.value


async def delete_media_objects(ctx: dict[str, Any], *, asset_ids: list[str]) -> int:
    """Удаляет из хранилища объекты удалённых и отклонённых ресурсов (идемпотентно)."""
    log = get_logger("messunjerr.jobs.media")
    settings = cast(Settings, ctx["settings"])
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    storage = cast(ObjectStorage, ctx["storage"])
    attempt = int(ctx.get("job_try", 1))
    link_lifetime = (
        timedelta(seconds=settings.upload_url_ttl_seconds) + media_housekeeping.LINK_LIFETIME_MARGIN
    )
    try:
        return await media_housekeeping.delete_media_objects(
            sessionmaker,
            storage,
            [uuid.UUID(item) for item in asset_ids],
            link_lifetime=link_lifetime,
        )
    except StorageUnavailableError as error:
        if attempt >= DELETE_MEDIA_OBJECTS_MAX_TRIES:
            # Объекты остались, строки тоже: `reconcile_uploads` поставит удаление заново.
            log.error("delete_media_objects_gave_up", count=len(asset_ids), attempts=attempt)
            raise
        delay = DELETE_MEDIA_OBJECTS_RETRY_BASE_SECONDS * 2 ** (attempt - 1)
        log.warning("delete_media_objects_retry", attempt=attempt, retry_in=delay)
        raise Retry(defer=delay) from error


async def cleanup_pending_uploads(ctx: dict[str, Any]) -> int:
    """Раз в час помечает удалёнными незавершённые загрузки старше суток (4.11)."""
    settings = cast(Settings, ctx["settings"])
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    jobs = cast(JobQueue, ctx["jobs"])
    return await media_housekeeping.cleanup_pending_uploads(
        sessionmaker, jobs, ttl=timedelta(hours=settings.pending_upload_ttl_hours)
    )


async def reconcile_uploads(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в несколько минут ставит заново потерянные задачи медиа (временная схема до Kafka)."""
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    jobs = cast(JobQueue, ctx["jobs"])
    result = await media_housekeeping.reconcile_uploads(sessionmaker, jobs)
    return {
        "processing_requeued": result.processing_requeued,
        "deletions_requeued": result.deletions_requeued,
    }


async def sweep_orphan_objects(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в сутки убирает объекты без живого ресурса и доделывает очистку оригиналов (4.11)."""
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    storage = cast(ObjectStorage, ctx["storage"])
    result = await media_housekeeping.sweep_orphan_objects(sessionmaker, storage)
    return {
        "scanned": result.scanned,
        "orphans_found": result.orphans_found,
        "removed": result.removed,
        "scrubbed": result.scrubbed,
    }


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
    """Удаляет токены из писем, записи `Idempotency-Key` и резервы прежних ников, срок которых вышел."""
    sessionmaker = cast("async_sessionmaker[AsyncSession]", ctx["sessionmaker"])
    removed = {
        "email_tokens": await purge_spent_tokens(sessionmaker),
        "idempotency_keys": await purge_expired_keys(sessionmaker),
        "username_reservations": await purge_username_reservations(sessionmaker),
    }
    # Ключ `email_tokens` фильтр журнала принял бы за секрет и скрыл: пишем под другим именем.
    get_logger("messunjerr.jobs.cleanup").info(
        "cleanup_tokens_and_idempotency",
        mail_links=removed["email_tokens"],
        idempotency_keys=removed["idempotency_keys"],
        username_reservations=removed["username_reservations"],
    )
    return removed

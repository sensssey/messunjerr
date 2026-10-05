"""Воркер фоновых задач на arq: по одному процессу на очередь (`email`, `default`, `media`).

arq 0.28 не умеет слушать несколько очередей в одном процессе, поэтому процесс обслуживает одну
очередь: `python -m messunjerr worker --queue email`. Запускаем воркер сами через
`asyncio.run(worker.async_run())`: штатный `arq` CLI берёт цикл событий через
`asyncio.get_event_loop()`, а в Python 3.14 это без запущенного цикла ошибка.

Очередь `default` держит плановые задачи (cron): их ставит и выполняет сам воркер. Если воркеров
`default` несколько, каждая плановая задача всё равно выполняется один раз за срабатывание:
идентификатор задачи arq включает время запуска.
"""

from datetime import UTC
from typing import Any

from arq.cron import CronJob, cron
from arq.worker import Function, Worker, func

from messunjerr.core.db import create_engine, create_sessionmaker
from messunjerr.core.jobs import (
    QUEUE_DEFAULT,
    QUEUE_EMAIL,
    QUEUE_MEDIA,
    TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
    TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
    TASK_SEND_EMAIL,
)
from messunjerr.core.logs import configure_logging, get_logger
from messunjerr.core.mail import SmtpMailer, parse_smtp_url
from messunjerr.jobs.health import queue_key
from messunjerr.jobs.queue import json_deserializer, json_serializer, redis_settings
from messunjerr.jobs.tasks import (
    SEND_EMAIL_MAX_TRIES,
    cleanup_tokens_and_idempotency,
    cleanup_unverified_accounts,
    send_email,
)
from messunjerr.settings import Settings, check_runtime, get_settings

HEALTH_CHECK_INTERVAL_SECONDS = 30
CLEANUP_TIMEOUT_SECONDS = 600


def functions_for(queue: str) -> list[Function]:
    """Задачи очереди. Результат не сохраняется (`keep_result=0`): в аргументах письма токены."""
    registry: dict[str, list[Function]] = {
        QUEUE_EMAIL: [
            func(
                send_email,
                name=TASK_SEND_EMAIL,
                max_tries=SEND_EMAIL_MAX_TRIES,
                keep_result=0,
                timeout=60,
            )
        ],
        QUEUE_DEFAULT: [],
        QUEUE_MEDIA: [],
    }
    return registry[queue]


def cron_jobs_for(queue: str) -> list[CronJob]:
    """Плановые задачи очереди; время по UTC (воркер собирается с `timezone=UTC`)."""
    registry: dict[str, list[CronJob]] = {
        QUEUE_EMAIL: [],
        QUEUE_DEFAULT: [
            # Раз в сутки ночью: аккаунты без подтверждённой почты занимают ник и адрес.
            cron(
                cleanup_unverified_accounts,
                name=TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
                hour=3,
                minute=10,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            ),
            # Раз в час: токены писем и записи идемпотентности быстро теряют смысл.
            cron(
                cleanup_tokens_and_idempotency,
                name=TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
                minute=17,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            ),
        ],
        QUEUE_MEDIA: [],
    }
    return registry[queue]


async def _on_startup(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx.get("settings") or get_settings()
    queue = ctx.get("queue")
    configure_logging(settings.log_level, settings.log_format)
    ctx["settings"] = settings
    if queue == QUEUE_EMAIL:
        check_runtime(settings, needs_mail=True)
        if settings.smtp_url is None:
            raise RuntimeError("Для воркера почты нужен SMTP_URL (или SMTP_URL_FILE)")
        ctx["mailer"] = SmtpMailer(parse_smtp_url(settings.smtp_url.get_secret_value()))
    else:
        check_runtime(settings)
        engine = create_engine(settings)
        ctx["engine"] = engine
        ctx["sessionmaker"] = create_sessionmaker(engine)
    get_logger("messunjerr.jobs").info("worker_started", queue=queue)


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    get_logger("messunjerr.jobs").info("worker_stopped")


def build_worker(
    queue: str, settings: Settings, *, burst: bool = False, handle_signals: bool = True
) -> Worker:
    """Собирает воркер очереди. `burst=True` обрабатывает накопленное и выходит (тесты)."""
    functions = functions_for(queue)
    cron_jobs = cron_jobs_for(queue)
    if not functions and not cron_jobs:
        raise RuntimeError(f"В очереди {queue!r} пока нет задач: воркер ей не нужен")
    return Worker(
        functions=functions,
        cron_jobs=cron_jobs,
        queue_name=queue_key(queue),
        redis_settings=redis_settings(settings.redis_url.get_secret_value()),
        on_startup=_on_startup,
        on_shutdown=_on_shutdown,
        job_serializer=json_serializer,
        job_deserializer=json_deserializer,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        burst=burst,
        handle_signals=handle_signals,
        timezone=UTC,
        ctx={"queue": queue, "settings": settings},
    )


async def run_worker(queue: str) -> None:
    worker = build_worker(queue, get_settings())
    await worker.async_run()


__all__ = ["build_worker", "cron_jobs_for", "functions_for", "run_worker"]

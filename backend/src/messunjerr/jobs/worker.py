"""Воркер фоновых задач на arq: по одному процессу на очередь (`email`, `default`, `media`).

arq 0.28 не умеет слушать несколько очередей в одном процессе, поэтому процесс обслуживает одну
очередь: `python -m messunjerr worker --queue email`. Запускаем воркер сами через
`asyncio.run(worker.async_run())`: штатный `arq` CLI берёт цикл событий через
`asyncio.get_event_loop()`, а в Python 3.14 это без запущенного цикла ошибка.
"""

from typing import Any

from arq.worker import Function, Worker, func
from redis.asyncio import Redis

from messunjerr.core.jobs import QUEUE_DEFAULT, QUEUE_EMAIL, QUEUE_MEDIA, TASK_SEND_EMAIL
from messunjerr.core.logs import configure_logging, get_logger
from messunjerr.core.mail import SmtpMailer, parse_smtp_url
from messunjerr.jobs.queue import json_deserializer, json_serializer, queue_key, redis_settings
from messunjerr.jobs.tasks import SEND_EMAIL_MAX_TRIES, send_email
from messunjerr.settings import Settings, check_runtime, get_settings

HEALTH_CHECK_INTERVAL_SECONDS = 30


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


async def _on_startup(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx.get("settings") or get_settings()
    check_runtime(settings, needs_mail=True)
    configure_logging(settings.log_level, settings.log_format)
    if settings.smtp_url is None:
        raise RuntimeError("Для воркера почты нужен SMTP_URL (или SMTP_URL_FILE)")
    ctx["settings"] = settings
    ctx["mailer"] = SmtpMailer(parse_smtp_url(settings.smtp_url.get_secret_value()))
    get_logger("messunjerr.jobs").info("worker_started", queue=ctx.get("queue"))


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    get_logger("messunjerr.jobs").info("worker_stopped")


def build_worker(
    queue: str, settings: Settings, *, burst: bool = False, handle_signals: bool = True
) -> Worker:
    """Собирает воркер очереди. `burst=True` обрабатывает накопленное и выходит (тесты)."""
    functions = functions_for(queue)
    if not functions:
        raise RuntimeError(f"В очереди {queue!r} пока нет задач: воркер ей не нужен")
    return Worker(
        functions=functions,
        queue_name=queue_key(queue),
        redis_settings=redis_settings(settings.redis_url.get_secret_value()),
        on_startup=_on_startup,
        on_shutdown=_on_shutdown,
        job_serializer=json_serializer,
        job_deserializer=json_deserializer,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        burst=burst,
        handle_signals=handle_signals,
        ctx={"queue": queue, "settings": settings},
    )


async def run_worker(queue: str) -> None:
    worker = build_worker(queue, get_settings())
    await worker.async_run()


async def worker_is_alive(queue: str, redis_url: str) -> bool:
    """Для HEALTHCHECK контейнера: воркер периодически обновляет ключ состояния в Redis."""
    client: Redis = Redis.from_url(redis_url)  # pyright: ignore[reportUnknownMemberType]
    try:
        return bool(
            await client.exists(f"{queue_key(queue)}:health-check")  # pyright: ignore[reportUnknownMemberType]
        )
    except Exception:
        return False
    finally:
        await client.aclose()


__all__ = ["build_worker", "functions_for", "run_worker", "worker_is_alive"]

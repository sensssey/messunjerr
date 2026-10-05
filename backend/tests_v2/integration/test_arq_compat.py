"""Сторож совместимости arq 0.28 и redis-py 8.1 (риск R2 спецификации).

arq заявляет `redis<6`, но проект работает на redis-py 8.1: пин снят в `[tool.uv]`. Если очередное
обновление зависимостей сломает эту связку, этот тест упадёт раньше, чем письма перестанут уходить.
"""

import uuid
from datetime import timedelta
from typing import Any

from arq.worker import Retry, Worker, func

from messunjerr.jobs.queue import (
    ArqJobQueue,
    json_deserializer,
    json_serializer,
    queue_key,
    redis_settings,
)
from messunjerr.settings import Settings


async def test_enqueue_dedupe_defer_and_retry_work_with_this_redis_client(
    test_settings: Settings,
) -> None:
    url = test_settings.redis_url.get_secret_value()
    queue_name = f"compat-{uuid.uuid4().hex[:8]}"
    queue = ArqJobQueue(url)
    seen: list[str] = []

    async def echo(ctx: dict[str, Any], *, text: str) -> str:
        seen.append(text)
        return text

    async def flaky(ctx: dict[str, Any]) -> None:
        if ctx["job_try"] < 3:
            raise Retry(defer=0.05)
        seen.append(f"flaky ok after {ctx['job_try']} tries")

    try:
        assert await queue.enqueue("echo", queue=queue_name, job_id="one", text="привет") is True
        assert await queue.enqueue("echo", queue=queue_name, job_id="one", text="дубль") is False
        assert await queue.enqueue("flaky", queue=queue_name) is True
        assert (
            await queue.enqueue(
                "echo", queue=queue_name, defer_by=timedelta(milliseconds=300), text="позже"
            )
            is True
        )

        worker = Worker(
            functions=[
                func(echo, name="echo", keep_result=0),
                func(flaky, name="flaky", max_tries=5, keep_result=0),
            ],
            queue_name=queue_key(queue_name),
            redis_settings=redis_settings(url),
            burst=True,
            poll_delay=0.05,
            handle_signals=False,
            health_check_interval=0,
            job_serializer=json_serializer,
            job_deserializer=json_deserializer,
        )
        await worker.async_run()
        await worker.close()
    finally:
        await queue.close()

    assert sorted(seen) == sorted(["привет", "позже", "flaky ok after 3 tries"])

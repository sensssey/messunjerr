"""Порт очереди фоновых задач (4.12).

Команды ставят задачи через `JobQueue` и не знают, что за ним стоит: сейчас arq
(`messunjerr.jobs`), при необходимости Taskiq или Procrastinate (риск R2 спецификации).
Аргументы задач обязаны сериализоваться в JSON (так хранит и arq): только строки, числа, списки и
словари. Ни токенов доступа, ни паролей в задачи не кладём.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

QUEUE_EMAIL = "email"
QUEUE_DEFAULT = "default"
QUEUE_MEDIA = "media"
QUEUES: tuple[str, ...] = (QUEUE_EMAIL, QUEUE_DEFAULT, QUEUE_MEDIA)

# Имена задач: общий договор между теми, кто ставит задачу (контексты), и воркером.
TASK_SEND_EMAIL = "send_email"
TASK_CLEANUP_UNVERIFIED_ACCOUNTS = "cleanup_unverified_accounts"
TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY = "cleanup_tokens_and_idempotency"
TASK_PROCESS_MEDIA = "process_media"
TASK_DELETE_MEDIA_OBJECTS = "delete_media_objects"
TASK_CLEANUP_PENDING_UPLOADS = "cleanup_pending_uploads"
TASK_RECONCILE_UPLOADS = "reconcile_uploads"
TASK_SWEEP_ORPHAN_OBJECTS = "sweep_orphan_objects"


class JobQueue(Protocol):
    async def enqueue(
        self,
        name: str,
        *,
        queue: str = QUEUE_DEFAULT,
        job_id: str | None = None,
        defer_by: timedelta | None = None,
        **kwargs: Any,
    ) -> bool:
        """Ставит задачу `name` в очередь `queue`.

        `job_id` делает постановку идемпотентной: повтор с тем же идентификатором возвращает
        `False` и дубль не создаёт. `defer_by` откладывает запуск.
        """
        ...


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    name: str
    queue: str
    job_id: str | None
    defer_by: timedelta | None
    kwargs: dict[str, Any]


@dataclass(slots=True)
class InMemoryJobQueue:
    """Очередь в памяти для тестов: запоминает задачи и, как arq, отбрасывает дубли `job_id`."""

    jobs: list[EnqueuedJob] = field(default_factory=list[EnqueuedJob])
    fail_with: Exception | None = None
    """Если задано, `enqueue` выбрасывает это исключение (имитация недоступного Redis)."""
    _seen_ids: set[str] = field(default_factory=set[str])

    async def enqueue(
        self,
        name: str,
        *,
        queue: str = QUEUE_DEFAULT,
        job_id: str | None = None,
        defer_by: timedelta | None = None,
        **kwargs: Any,
    ) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        if job_id is not None:
            if job_id in self._seen_ids:
                return False
            self._seen_ids.add(job_id)
        self.jobs.append(EnqueuedJob(name, queue, job_id, defer_by, dict(kwargs)))
        return True

    def named(self, name: str) -> list[EnqueuedJob]:
        return [job for job in self.jobs if job.name == name]

    def clear(self) -> None:
        self.jobs.clear()
        self._seen_ids.clear()

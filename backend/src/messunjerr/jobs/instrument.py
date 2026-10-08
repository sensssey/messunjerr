"""Метрики задач воркера (4.15): запуски по исходу, ошибки, длительность.

Обёртка вешается на функцию задачи при сборке списка задач воркера и ничего не меняет в её работе:
исключение летит дальше, где его ждёт arq (повтор `Retry`, учёт ошибки, журнал). Отмена
(`CancelledError`: тайм-аут задачи или остановка воркера) не считается ошибкой задачи.
"""

import functools
import time
from collections.abc import Awaitable, Callable
from types import CoroutineType
from typing import Any

from arq.worker import Retry

from messunjerr.core.metrics import ARQ_JOB_SECONDS, ARQ_JOBS, ARQ_JOBS_FAILED


def instrumented[**P, T](
    name: str, task: Callable[P, Awaitable[T]]
) -> Callable[P, CoroutineType[Any, Any, T]]:
    """Задача `task` с учётом в метриках под именем `name` (метка `job`)."""

    @functools.wraps(task)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        started = time.perf_counter()
        try:
            result = await task(*args, **kwargs)
        except Retry:
            ARQ_JOBS.labels(job=name, outcome="retried").inc()
            raise
        except Exception:
            ARQ_JOBS.labels(job=name, outcome="failed").inc()
            ARQ_JOBS_FAILED.labels(job=name).inc()
            raise
        else:
            ARQ_JOBS.labels(job=name, outcome="completed").inc()
            return result
        finally:
            ARQ_JOB_SECONDS.labels(job=name).observe(time.perf_counter() - started)

    return wrapper

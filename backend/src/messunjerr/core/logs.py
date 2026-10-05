"""Логи: structlog, JSON в stdout, общие поля из contextvars (`request_id`, позже `user_id`).

Тела запросов, пароли, токены и cookie в лог не попадают: процессор `redact` скрывает значения
чувствительных ключей даже тогда, когда кто-то по ошибке передал их в событие.
"""

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|passw|secret|token|ticket|refresh|api[_-]?key", re.IGNORECASE
)
_REDACTED = "[REDACTED]"


class _CurrentStdoutHandler(logging.Handler):
    """Пишет в актуальный `sys.stdout`, а не в тот, что был на момент настройки.

    Так же устроен стандартный `lastResort`-обработчик: перенаправление stdout (pytest, тесты)
    не оставляет логи в «устаревшем» потоке.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stdout
            stream.write(self.format(record) + "\n")
            stream.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def redact(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Скрывает значения ключей, похожих на секреты (`authorization`, `token`, `password`…)."""
    for key in list(event_dict):
        if key != "event" and _SENSITIVE_KEY.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(
    level: str = "INFO", fmt: str = "json", *, cache_loggers: bool = True
) -> None:
    """Настраивает structlog и перенаправляет в него стандартный `logging` (uvicorn, SQLAlchemy).

    `cache_loggers=False` нужен тестам, которые настраивают логи заново в каждом тесте.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        redact,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=cache_loggers,
    )

    final: list[Processor]
    if fmt == "console":
        final = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ]
    else:
        final = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]

    handler = _CurrentStdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=final)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Запросы логирует наше middleware, поэтому access-лог uvicorn отключаем.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)

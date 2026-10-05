"""Командная строка: `python -m messunjerr <команда>` (в образе также `messunjerr`)."""

import argparse
import asyncio
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from alembic import command

from messunjerr.core.dbinit import init_database, plan_from_settings
from messunjerr.core.jobs import QUEUE_EMAIL, QUEUES
from messunjerr.core.migrations import alembic_config
from messunjerr.settings import get_settings


def _serve(args: argparse.Namespace) -> int:
    import uvicorn  # тяжёлый импорт нужен только этой команде

    uvicorn.run(
        "messunjerr.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,
        log_config=None,
    )
    return 0


def _db_init(_: argparse.Namespace) -> int:
    asyncio.run(init_database(plan_from_settings(get_settings())))
    print("db-init: роли и база готовы")
    return 0


def _migrate(_: argparse.Namespace) -> int:
    command.upgrade(alembic_config(), "head")
    print("migrate: схема на последней ревизии")
    return 0


def _bootstrap(args: argparse.Namespace) -> int:
    _db_init(args)
    return _migrate(args)


def _healthcheck(args: argparse.Namespace) -> int:
    """Для HEALTHCHECK контейнера: код 0, если процесс отвечает на /health/live."""
    try:
        with urllib.request.urlopen(args.url, timeout=3) as response:  # noqa: S310 (адрес задаёт оператор)
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


def _worker(args: argparse.Namespace) -> int:
    """Воркер фоновых задач (arq): один процесс на очередь."""
    from messunjerr.jobs.worker import run_worker, worker_is_alive  # arq нужен только воркеру

    queue: str = args.queue
    if args.check:
        redis_url = get_settings().redis_url.get_secret_value()
        return 0 if asyncio.run(worker_is_alive(queue, redis_url)) else 1
    if args.reload:
        from watchfiles import run_process

        package_dir = str(Path(__file__).resolve().parent)
        run_process(
            package_dir,
            target=f"{sys.executable} -m messunjerr worker --queue {queue}",
            target_type="command",
        )
        return 0
    asyncio.run(run_worker(queue))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="messunjerr", description="Бэкенд messunjerr v2")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="запустить API (uvicorn)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(handler=_serve)

    sub.add_parser("db-init", help="создать роли и базу (нужен ADMIN_DATABASE_URL)").set_defaults(
        handler=_db_init
    )
    sub.add_parser("migrate", help="применить миграции до head").set_defaults(handler=_migrate)
    sub.add_parser("bootstrap", help="db-init и migrate").set_defaults(handler=_bootstrap)

    health = sub.add_parser("healthcheck", help="проверить /health/live для HEALTHCHECK")
    health.add_argument("--url", default="http://127.0.0.1:8000/health/live")
    health.set_defaults(handler=_healthcheck)

    worker = sub.add_parser("worker", help="запустить воркер фоновых задач (arq)")
    worker.add_argument(
        "--queue", choices=QUEUES, default=QUEUE_EMAIL, help="очередь (по умолчанию email)"
    )
    worker.add_argument(
        "--reload", action="store_true", help="перезапускать при правке кода (разработка)"
    )
    worker.add_argument("--check", action="store_true", help="HEALTHCHECK: код 0, если воркер жив")
    worker.set_defaults(handler=_worker)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())

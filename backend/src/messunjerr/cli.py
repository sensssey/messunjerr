"""Командная строка: `python -m messunjerr <команда>` (в образе также `messunjerr`)."""

import argparse
import asyncio
import getpass
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from messunjerr.core.jobs import QUEUE_EMAIL, QUEUES
from messunjerr.settings import get_settings

# Тяжёлые импорты (alembic, SQLAlchemy, uvicorn, arq) лежат внутри команд: `healthcheck` и
# `worker --check` запускаются Docker'ом каждые секунды и должны стартовать быстро.


def _serve(args: argparse.Namespace) -> int:
    if args.workers == 1 and not args.reload:
        # Боевой путь: один процесс, мягкая остановка со сливом трафика (S4-03).
        from messunjerr.core.server import run_server

        run_server(get_settings(), host=args.host, port=args.port)
        return 0

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
    from messunjerr.core.dbinit import init_database, plan_from_settings

    asyncio.run(init_database(plan_from_settings(get_settings())))
    print("db-init: роли и база готовы")
    return 0


def _migrate(_: argparse.Namespace) -> int:
    from alembic import command

    from messunjerr.core.migrations import alembic_config, database_is_ahead

    url = get_settings().migrator_database_url
    if url is not None and asyncio.run(database_is_ahead(url.get_secret_value())):
        # Откат кода после выкладки с миграцией: схема совместима с обеими версиями (4.16), менять нечего.
        print("migrate: БД новее кода (её ревизии нет в этой сборке): миграции не применяются")
        return 0
    command.upgrade(alembic_config(), "head")
    print("migrate: схема на последней ревизии")
    return 0


def _bootstrap(args: argparse.Namespace) -> int:
    _db_init(args)
    return _migrate(args)


def _seed(args: argparse.Namespace) -> int:
    from messunjerr.seeding import (  # тяжёлые импорты только этой команде
        SEED_PASSWORD,
        seed_database,
    )

    password: str = args.password or SEED_PASSWORD
    try:
        result = asyncio.run(seed_database(get_settings(), users=args.users, password=password))
    except RuntimeError as error:
        print(f"seed: {error}", file=sys.stderr)
        return 1
    print(f"seed: создано аккаунтов {result.created}, уже было {result.existing}")
    print(f"seed: вход под seed_0001 … seed_{args.users:04d}, пароль {password}")
    return 0


def _seed_big(args: argparse.Namespace) -> int:
    from messunjerr.seeding import (  # тяжёлые импорты только этой команде
        SEED_PASSWORD,
        seed_big_database,
    )

    password: str = args.password or SEED_PASSWORD
    try:
        result = asyncio.run(
            seed_big_database(get_settings(), users=args.users, password=password, seed=args.seed)
        )
    except (RuntimeError, ValueError) as error:
        print(f"seed-big: {error}", file=sys.stderr)
        return 1
    print(f"seed-big: создано людей {result.created}, уже было {result.existing}")
    print(
        f"seed-big: добавлено дружб {result.friendships}, подписок {result.follows}, "
        f"блокировок {result.blocks}, заявок в друзья {result.friend_requests}, "
        f"запросов на подписку {result.follow_requests}"
    )
    for section, count in result.extras.items():
        print(f"seed-big: {section}: {count}")
    print(
        f"seed-big: вход под big_00001 … big_{args.users:05d}, пароль {password}; "
        f"заняло {result.seconds:.1f} с"
    )
    print(
        "seed-big: статистику планировщика обновит автоочистка в течение минуты; для замеров планов "
        "сразу после посева выполните ANALYZE от имени владельца таблиц"
    )
    return 0


def _create_admin(args: argparse.Namespace) -> int:
    from messunjerr.admin import (  # тяжёлые импорты только этой команде
        AdminError,
        PasswordRequiredError,
        create_admin,
    )
    from messunjerr.tables import register_tables

    register_tables()  # профиль ссылается на другие схемы: без их таблиц запись не соберётся
    settings = get_settings()
    # Пароль не принимается аргументом командной строки: он остался бы в истории оболочки.
    password: str | None = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else os.environ.get("ADMIN_PASSWORD") or None
    )
    try:
        try:
            result = asyncio.run(
                create_admin(settings, email=args.email, username=args.username, password=password)
            )
        except PasswordRequiredError:
            if not sys.stdin.isatty():
                raise
            first = getpass.getpass("Пароль нового администратора: ")
            if first != getpass.getpass("Ещё раз: "):
                raise AdminError("пароли не совпали") from None
            result = asyncio.run(
                create_admin(settings, email=args.email, username=args.username, password=first)
            )
    except AdminError as error:
        print(f"create-admin: {error}", file=sys.stderr)
        return 1
    action = "создан администратор" if result.created else "роль admin выдана аккаунту"
    print(f"create-admin: {action} {result.username} ({result.user_id})")
    if result.ignored:
        print(
            f"create-admin: предупреждение: не применено: {', '.join(result.ignored)} "
            "(у существующего аккаунта меняется только роль)",
            file=sys.stderr,
        )
    return 0


def _reprocess_media(_: argparse.Namespace) -> int:
    from messunjerr.core.db import create_engine, create_sessionmaker  # тяжёлые импорты
    from messunjerr.jobs.queue import ArqJobQueue
    from messunjerr.media.commands.reprocess import reprocess_legacy_images
    from messunjerr.tables import register_tables

    register_tables()  # ресурс ссылается на identity.users: без неё первая же запись падает

    async def run() -> int:
        settings = get_settings()
        engine = create_engine(settings)
        jobs = ArqJobQueue(settings.redis_url.get_secret_value())
        try:
            return await reprocess_legacy_images(create_sessionmaker(engine), jobs)
        finally:
            await jobs.close()
            await engine.dispose()

    print(f"reprocess-media: на обработку возвращено ресурсов: {asyncio.run(run())}")
    return 0


def _healthcheck(args: argparse.Namespace) -> int:
    """Для HEALTHCHECK контейнера: код 0, если процесс отвечает на /health/live."""
    try:
        with urllib.request.urlopen(args.url, timeout=3) as response:  # noqa: S310 (адрес задаёт оператор)
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


def _worker(args: argparse.Namespace) -> int:
    """Воркер фоновых задач (arq): один процесс на очередь."""
    queue: str = args.queue
    if args.check:
        # Лёгкий модуль: проверка идёт каждые секунды и не должна тянуть arq и всё приложение.
        from messunjerr.jobs.health import worker_is_alive

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
    from messunjerr.jobs.worker import run_worker  # arq нужен только воркеру

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

    seed = sub.add_parser(
        "seed", help="создать учебные аккаунты с профилями (только dev и test, повтор безопасен)"
    )
    seed.add_argument("--users", type=int, default=30, help="сколько аккаунтов (по умолчанию 30)")
    seed.add_argument("--password", default=None, help="общий пароль (по умолчанию учебный)")
    seed.set_defaults(handler=_seed)

    seed_big = sub.add_parser(
        "seed-big",
        help="большой набор для замеров: люди big_00001…, друзья, подписки, блокировки, заявки "
        "(только dev и test, повтор безопасен)",
    )
    seed_big.add_argument(
        "--users", type=int, default=5000, help="сколько людей (по умолчанию 5000)"
    )
    seed_big.add_argument("--password", default=None, help="общий пароль (по умолчанию учебный)")
    seed_big.add_argument(
        "--seed", type=int, default=2026, help="зерно генератора: то же зерно даёт те же данные"
    )
    seed_big.set_defaults(handler=_seed_big)

    admin = sub.add_parser(
        "create-admin",
        help="создать администратора или выдать роль существующему аккаунту (по почте)",
    )
    admin.add_argument("--email", required=True)
    admin.add_argument("--username", required=True)
    admin.add_argument(
        "--password-stdin",
        action="store_true",
        help="прочитать пароль новой учётной записи из stdin (иначе ADMIN_PASSWORD или запрос)",
    )
    admin.set_defaults(handler=_create_admin)

    sub.add_parser(
        "reprocess-media",
        help="вернуть на обработку готовые изображения без вариантов (ресурсы времён S5)",
    ).set_defaults(handler=_reprocess_media)

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

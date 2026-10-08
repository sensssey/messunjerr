"""Команды оператора (S6-07): `create-admin` и `reprocess-media`.

`seed` проверяет `test_seed.py`. Команды командной строки сами запускают цикл событий, поэтому из
теста их зовут в потоке: внутри работающего цикла `asyncio.run` не разрешён.
"""

import asyncio
import io
import os
import subprocess
import sys
import uuid
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr import cli
from messunjerr.admin import AdminError, PasswordRequiredError, create_admin
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.jobs.health import queue_key
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.settings import Settings

from .helpers import LOGIN, ME, execute, fetch_all, fetch_one, verified_user
from .media_helpers import JPEG, uploaded

PASSWORD = "operator pass phrase 2026"


async def audit_of(engine: AsyncEngine) -> list[dict[str, Any]]:
    return await fetch_all(
        engine,
        "SELECT action, actor_id, target_type, target_id, data FROM platform.audit_log "
        "WHERE action = 'role.changed' ORDER BY id",
    )


async def test_a_new_administrator_is_created_ready_to_sign_in(
    client: httpx.AsyncClient, test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    result = await create_admin(
        test_settings, email="  Root@Example.COM ", username="Root_Operator", password=PASSWORD
    )

    assert result.created
    assert result.username == "root_operator"
    user = await fetch_one(
        admin_engine, "SELECT * FROM identity.users WHERE id = :id", id=result.user_id
    )
    assert (user["role"], user["status"], user["email"], user["username"]) == (
        "admin",
        "active",
        "root@example.com",
        "root_operator",
    )
    assert user["email_verified_at"] is not None
    assert user["terms_version"] == test_settings.legal_terms_version
    # Профиль и настройки приватности есть, как у любого аккаунта.
    profile = await fetch_one(
        admin_engine,
        "SELECT display_name FROM profile.profiles WHERE user_id = :id",
        id=result.user_id,
    )
    assert profile["display_name"] == "root_operator"
    await fetch_one(
        admin_engine,
        "SELECT 1 FROM profile.privacy_settings WHERE user_id = :id",
        id=result.user_id,
    )
    (entry,) = await audit_of(admin_engine)
    assert entry["actor_id"] is None
    assert str(entry["target_id"]) == str(result.user_id)
    assert entry["data"] == {"from": None, "to": "admin", "via": "cli", "created": True}
    assert PASSWORD not in str(entry)

    signed_in = await client.post(LOGIN, json={"login": "root@example.com", "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    me = await client.get(
        ME, headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"}
    )
    assert me.json()["role"] == "admin"


async def test_the_operator_may_take_a_reserved_name(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    result = await create_admin(
        test_settings, email="owner@example.com", username="admin", password=PASSWORD
    )

    assert result.username == "admin"
    assert (
        await fetch_one(admin_engine, "SELECT role FROM identity.users WHERE username = 'admin'")
    )["role"] == "admin"


async def test_an_existing_account_is_promoted_by_its_email_and_the_password_is_kept(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    person = await verified_user(client, jobs)
    credentials = person.credentials

    result = await create_admin(
        test_settings, email=credentials["email"], username="ignored_name", password=None
    )

    assert (result.created, result.user_id) == (False, uuid.UUID(person.user_id))
    assert result.username == credentials["username"]  # ник не меняется, имя из команды не берётся
    row = await fetch_one(
        admin_engine, "SELECT role FROM identity.users WHERE id = :id", id=result.user_id
    )
    assert row["role"] == "admin"
    (entry,) = await audit_of(admin_engine)
    assert entry["data"] == {"from": "user", "to": "admin", "via": "cli"}
    # Пароль прежний: команда его не трогает.
    again = await client.post(
        LOGIN, json={"login": credentials["email"], "password": credentials["password"]}
    )
    assert again.status_code == 200


async def test_a_password_and_a_name_given_for_an_existing_account_are_reported_not_applied(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    """Дефект из ревью: пароль хэшировался и молча отбрасывался, оператор думал, что сменил его."""
    person = await verified_user(client, jobs)
    credentials = person.credentials
    before = await fetch_one(
        admin_engine,
        "SELECT password_hash FROM identity.users WHERE id = :id",
        id=uuid.UUID(person.user_id),
    )

    result = await create_admin(
        test_settings, email=credentials["email"], username="another_name", password=PASSWORD
    )

    assert result.ignored == ("пароль", "ник")
    after = await fetch_one(
        admin_engine,
        "SELECT password_hash, username FROM identity.users WHERE id = :id",
        id=result.user_id,
    )
    assert after["password_hash"] == before["password_hash"]
    assert after["username"] == credentials["username"]
    # Ник, совпадающий с прежним, и отсутствие пароля предупреждений не вызывают.
    quiet = await create_admin(
        test_settings, email=credentials["email"], username=credentials["username"], password=None
    )
    assert quiet.ignored == ()


async def test_giving_the_role_twice_is_harmless(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    first = await create_admin(
        test_settings, email="boss@example.com", username="the_boss", password=PASSWORD
    )

    second = await create_admin(
        test_settings, email="boss@example.com", username="the_boss", password=None
    )

    assert (first.created, second.created) == (True, False)
    assert first.user_id == second.user_id
    count = await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users")
    assert count["n"] == 1


@pytest.mark.parametrize(
    ("email", "username", "password", "message"),
    [
        ("not-an-email", "someone", PASSWORD, "почты"),
        ("a@example.com", "ab", PASSWORD, "ник"),
        ("a@example.com", "bad name!", PASSWORD, "ник"),
        ("a@example.com", "someone", "short", "пароль"),
        ("a@example.com", "someone", "qwertyuiop", "пароль"),  # из списка частых
        ("a@example.com", "someone", "someone", "пароль"),
    ],
)
async def test_bad_input_is_refused_with_a_message_for_the_operator(
    test_settings: Settings,
    admin_engine: AsyncEngine,
    email: str,
    username: str,
    password: str,
    message: str,
) -> None:
    with pytest.raises(AdminError, match=message):
        await create_admin(test_settings, email=email, username=username, password=password)

    assert (await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users"))["n"] == 0


async def test_a_new_account_needs_a_password(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    with pytest.raises(PasswordRequiredError):
        await create_admin(
            test_settings, email="new@example.com", username="newcomer", password=None
        )

    assert (await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users"))["n"] == 0


async def test_a_name_taken_by_another_account_is_refused(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, test_settings: Settings
) -> None:
    person = await verified_user(client, jobs)

    with pytest.raises(AdminError, match="занят"):
        await create_admin(
            test_settings,
            email="other@example.com",
            username=person.credentials["username"],
            password=PASSWORD,
        )
    other = await verified_user(client, jobs)
    with pytest.raises(AdminError, match="занят"):
        await create_admin(
            test_settings,
            email=other.credentials["email"],
            username=person.credentials["username"],  # ник другого человека при чужой почте
            password=None,
        )


@pytest.mark.parametrize("status", ["suspended", "banned", "pending", "deletion_pending"])
async def test_the_role_is_given_only_to_active_accounts(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    test_settings: Settings,
    admin_engine: AsyncEngine,
    status: str,
) -> None:
    person = await verified_user(client, jobs)
    await execute(
        admin_engine,
        "UPDATE identity.users SET status = :status WHERE id = :id",
        status=status,
        id=uuid.UUID(person.user_id),
    )

    with pytest.raises(AdminError, match=status):
        await create_admin(
            test_settings, email=person.credentials["email"], username="whoever", password=None
        )

    row = await fetch_one(
        admin_engine, "SELECT role FROM identity.users WHERE id = :id", id=uuid.UUID(person.user_id)
    )
    assert row["role"] == "user"


# ----------------------------------------------------------------------------- командная строка
async def test_the_command_reads_the_password_from_stdin_and_never_prints_it(
    test_settings: Settings,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))

    code = await asyncio.to_thread(
        cli.main,
        [
            "create-admin",
            "--email",
            "cli@example.com",
            "--username",
            "from_cli",
            "--password-stdin",
        ],
    )

    out = capsys.readouterr()
    assert code == 0, out.err
    assert "создан администратор from_cli" in out.out
    assert PASSWORD not in out.out + out.err
    assert (
        await fetch_one(admin_engine, "SELECT role FROM identity.users WHERE username = 'from_cli'")
    )["role"] == "admin"


async def test_the_command_takes_the_password_from_the_environment(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    code = await asyncio.to_thread(
        cli.main, ["create-admin", "--email", "env@example.com", "--username", "from_env"]
    )

    assert code == 0, capsys.readouterr().err


async def test_the_command_reports_problems_and_exits_with_a_code(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # не терминал: спрашивать пароль некому

    code = await asyncio.to_thread(
        cli.main, ["create-admin", "--email", "none@example.com", "--username", "no_password"]
    )

    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("create-admin: ")
    assert "нужен пароль" in err


async def test_reprocess_media_returns_old_ready_images_to_the_queue(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    redis_client: Redis,
    test_settings: Settings,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)

    code = await asyncio.to_thread(cli.main, ["reprocess-media"])

    assert code == 0
    assert "возвращено ресурсов: 1" in capsys.readouterr().out
    row = await fetch_one(
        admin_engine,
        "SELECT status FROM media.assets WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )
    assert row["status"] == "uploaded"
    assert await redis_client.zcard(queue_key("media")) == 1  # pyright: ignore[reportGeneralTypeIssues, reportUnknownMemberType, reportUnknownVariableType]


# ----------------------------------------------------------------------------- настоящие процессы
def run_command(
    settings: Settings, *args: str, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    """Команда так, как её запускает оператор: отдельный процесс, в котором загружено только нужное ей.

    Внутри процесса тестов уже импортировано всё приложение, и забытая регистрация таблиц (внешние
    ключи между схемами) не проявляется; отдельный процесс её ловит.
    """
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "REDIS_URL": settings.redis_url.get_secret_value(),
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "json",
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_COST_KIB": "1024",
        "ARGON2_PARALLELISM": "1",
    }
    return subprocess.run(  # noqa: S603 (запускаем собственный модуль)
        [sys.executable, "-m", "messunjerr", *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def test_create_admin_works_as_a_real_process(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    done = await asyncio.to_thread(
        run_command,
        test_settings,
        "create-admin",
        "--email",
        "process@example.com",
        "--username",
        "process_admin",
        "--password-stdin",
        stdin=PASSWORD + "\n",
    )

    assert done.returncode == 0, done.stderr
    assert "создан администратор process_admin" in done.stdout
    assert PASSWORD not in done.stdout + done.stderr
    row = await fetch_one(
        admin_engine, "SELECT role FROM identity.users WHERE username = 'process_admin'"
    )
    assert row["role"] == "admin"
    await fetch_one(
        admin_engine,
        "SELECT 1 FROM profile.profiles p JOIN identity.users u ON u.id = p.user_id "
        "WHERE u.username = 'process_admin'",
    )


async def test_the_command_warns_when_it_ignores_a_password_for_an_existing_account(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    test_settings: Settings,
) -> None:
    person = await verified_user(client, jobs)

    done = await asyncio.to_thread(
        run_command,
        test_settings,
        "create-admin",
        "--email",
        person.credentials["email"],
        "--username",
        person.credentials["username"],
        "--password-stdin",
        stdin=PASSWORD + "\n",
    )

    assert done.returncode == 0, done.stderr
    assert "роль admin выдана аккаунту" in done.stdout
    assert "предупреждение: не применено: пароль (" in done.stderr
    assert "меняется только роль" in done.stderr
    assert PASSWORD not in done.stdout + done.stderr


async def test_reprocess_media_works_as_a_real_process(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    storage: InMemoryObjectStorage,
    redis_client: Redis,
    test_settings: Settings,
    admin_engine: AsyncEngine,
) -> None:
    user = await verified_user(client, jobs)
    created = await uploaded(client, user, storage, JPEG)
    await execute(
        admin_engine,
        "UPDATE media.assets SET status = 'ready' WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )

    done = await asyncio.to_thread(run_command, test_settings, "reprocess-media")

    assert done.returncode == 0, done.stderr
    assert "возвращено ресурсов: 1" in done.stdout
    row = await fetch_one(
        admin_engine,
        "SELECT status FROM media.assets WHERE id = :id",
        id=uuid.UUID(created["asset"]["id"]),
    )
    assert row["status"] == "uploaded"
    assert await redis_client.zcard(queue_key("media")) == 1  # pyright: ignore[reportGeneralTypeIssues, reportUnknownMemberType, reportUnknownVariableType]


async def test_seed_works_as_a_real_process(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    done = await asyncio.to_thread(run_command, test_settings, "seed", "--users", "3")

    assert done.returncode == 0, done.stderr
    count = await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users")
    assert count["n"] == 3

"""Настройки, логирование, каталог кодов ошибок и границы модулей (import-linter)."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

from messunjerr.core.codes import PROBLEM_SPECS, ErrorCode, ItemCode
from messunjerr.core.logs import configure_logging, get_logger, redact
from messunjerr.settings import DEFAULT_REACTION_PALETTE, Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent
DB_URL = "postgresql+asyncpg://app:secret@db:5432/messunjerr"
REDIS_URL = "redis://:secret@redis:6379/0"

_ENV_KEYS = (
    "APP_ENV", "DATABASE_URL", "DATABASE_URL_FILE", "REDIS_URL", "REDIS_URL_FILE", "AUTH_METHODS",
    "REACTION_PALETTE", "MIGRATOR_DATABASE_URL", "ADMIN_DATABASE_URL", "LOG_LEVEL",
)  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def make_settings(**values: Any) -> Settings:
    return Settings(**values)  # pyright: ignore[reportCallIssue]


# ----------------------------------------------------------------------------- настройки
def test_settings_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", DB_URL)
    clean_env.setenv("REDIS_URL", REDIS_URL)
    clean_env.setenv("AUTH_METHODS", "password, vk ,yandex")
    clean_env.setenv("REACTION_PALETTE", "👍,❤️")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.app_env == "prod"  # по умолчанию безопасно
    assert settings.auth_methods == ["password", "vk", "yandex"]
    assert settings.reaction_palette == ["👍", "❤️"]
    assert settings.database_url.get_secret_value() == DB_URL


def test_settings_defaults() -> None:
    settings = make_settings(database_url=DB_URL, redis_url=REDIS_URL)
    assert settings.reaction_palette == list(DEFAULT_REACTION_PALETTE)
    assert settings.auth_methods == ["password"]
    assert settings.request_body_limit_bytes == 1_048_576


def test_secret_value_can_come_from_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "database_url"
    secret.write_text(DB_URL + "\n", encoding="utf-8")
    clean_env.setenv("DATABASE_URL_FILE", str(secret))
    clean_env.setenv("REDIS_URL", REDIS_URL)
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.database_url.get_secret_value() == DB_URL


def test_environment_wins_over_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "redis"
    secret.write_text("redis://:from-file@redis:6379/0", encoding="utf-8")
    clean_env.setenv("REDIS_URL_FILE", str(secret))
    clean_env.setenv("REDIS_URL", REDIS_URL)
    clean_env.setenv("DATABASE_URL", DB_URL)
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.redis_url.get_secret_value() == REDIS_URL


def test_missing_required_settings_fail_fast(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings()  # pyright: ignore[reportCallIssue]
    missing = {error["loc"][0] for error in caught.value.errors()}
    assert missing == {"database_url", "redis_url"}


@pytest.mark.parametrize(
    "bad_url", ["postgresql://u:p@h/db", "mysql://u:p@h/db", "postgres+asyncpg://u:p@h/db"]
)
def test_database_url_must_be_asyncpg(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url=bad_url, redis_url=REDIS_URL)


def test_redis_url_scheme_is_checked() -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url=DB_URL, redis_url="http://redis")


def test_docs_are_disabled_in_production_only() -> None:
    assert (
        make_settings(database_url=DB_URL, redis_url=REDIS_URL, app_env="prod").docs_enabled
        is False
    )
    assert (
        make_settings(database_url=DB_URL, redis_url=REDIS_URL, app_env="dev").docs_enabled is True
    )


def test_settings_are_immutable() -> None:
    settings = make_settings(database_url=DB_URL, redis_url=REDIS_URL)
    with pytest.raises(ValidationError):
        settings.app_env = "dev"  # pyright: ignore[reportAttributeAccessIssue]


def test_secret_is_not_printed() -> None:
    settings = make_settings(database_url=DB_URL, redis_url=REDIS_URL)
    # Пароль из адресов не печатается (в `repr` есть имена полей вроде s3_secret_key, но не значения).
    assert ":secret@" not in repr(settings)
    assert ":secret@" not in str(settings.model_dump())


# ----------------------------------------------------------------------------- логи
def test_redact_hides_sensitive_keys() -> None:
    event = {
        "event": "login",
        "password": "p",
        "Authorization": "Bearer abc",
        "refresh_token": "r",
        "user_id": "42",
        "set-cookie": "sid=1",
    }
    cleaned = redact(None, "info", event)
    assert cleaned["password"] == "[REDACTED]"
    assert cleaned["Authorization"] == "[REDACTED]"
    assert cleaned["refresh_token"] == "[REDACTED]"
    assert cleaned["set-cookie"] == "[REDACTED]"
    assert cleaned["user_id"] == "42"
    assert cleaned["event"] == "login"


@pytest.fixture
def captured_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[Any]:
    def read() -> list[dict[str, Any]]:
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    yield read
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_json_logs_have_context_and_hide_secrets(captured_logs: Any) -> None:
    configure_logging("INFO", "json", cache_loggers=False)
    structlog.contextvars.bind_contextvars(request_id="req-1")
    get_logger("test").info("hello", password="hunter2", answer=42, text="кириллица")
    (record,) = captured_logs()
    assert record["event"] == "hello"
    assert record["level"] == "info"
    assert record["request_id"] == "req-1"
    assert record["password"] == "[REDACTED]"
    assert record["answer"] == 42
    assert record["text"] == "кириллица"  # без \uXXXX
    assert record["ts"].endswith("Z") or "+00:00" in record["ts"]


def test_log_level_is_respected(captured_logs: Any) -> None:
    configure_logging("WARNING", "json", cache_loggers=False)
    log = get_logger("test")
    log.info("quiet")
    log.warning("loud")
    assert [r["event"] for r in captured_logs()] == ["loud"]


def test_exceptions_are_logged_as_structured_data(captured_logs: Any) -> None:
    configure_logging("INFO", "json", cache_loggers=False)
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("test").error("failed", exc_info=True)
    (record,) = captured_logs()
    assert record["exception"][0]["exc_type"] == "ValueError"


def test_console_format_is_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "console", cache_loggers=False)
    get_logger("test").info("hello", answer=42)
    out = capsys.readouterr().out
    structlog.reset_defaults()
    assert "hello" in out
    assert "answer=42" in out
    assert not out.lstrip().startswith("{")


# ----------------------------------------------------------------------------- каталог кодов
def test_every_error_code_has_a_problem_spec() -> None:
    assert set(PROBLEM_SPECS) == set(ErrorCode)
    for code, (status, title) in PROBLEM_SPECS.items():
        assert 400 <= status <= 599, code
        assert title


def test_codes_are_snake_case_and_unique() -> None:
    values = [code.value for code in ErrorCode] + [code.value for code in ItemCode]
    assert all(value == value.lower() and " " not in value for value in values)
    assert len({code.value for code in ErrorCode}) == len(ErrorCode)


def test_codes_match_the_spec_catalog() -> None:
    spec = REPO_DIR / "docs" / "backend-v2-spec.md"
    generator = REPO_DIR / "scripts" / "gen_error_codes.py"
    if not spec.exists() or not generator.exists():
        pytest.skip("документация недоступна (контейнер видит только backend/)")
    loader = importlib.util.spec_from_file_location("gen_error_codes", generator)
    assert loader is not None
    assert loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    problems, items = module.parse_catalog(spec.read_text(encoding="utf-8"))
    assert {code for _section, code, _status in problems} == {c.value for c in ErrorCode}
    assert {(code, status) for _s, code, status in problems} == {
        (code.value, status) for code, (status, _title) in PROBLEM_SPECS.items()
    }
    assert {code for _area, codes in items for code in codes} == {c.value for c in ItemCode}


# ----------------------------------------------------------------------------- границы модулей
def _lint_imports(project: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("lint-imports") or str(Path(sys.executable).parent / "lint-imports")
    env = {**os.environ, "PYTHONPATH": str(project / "src")}
    return subprocess.run(  # noqa: S603
        [executable, "--config", str(project / ".importlinter")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def project_copy(tmp_path: Path) -> Path:
    shutil.copytree(
        BACKEND_DIR / "src" / "messunjerr",
        tmp_path / "src" / "messunjerr",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(BACKEND_DIR / ".importlinter", tmp_path / ".importlinter")
    return tmp_path


def test_project_respects_import_contracts(project_copy: Path) -> None:
    result = _lint_imports(project_copy)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 6 kept, 0 broken" in result.stdout


def test_lower_context_importing_higher_context_breaks_the_build(project_copy: Path) -> None:
    (project_copy / "src" / "messunjerr" / "identity" / "violation.py").write_text(
        "from messunjerr import chat\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.parametrize(
    ("layer", "module"),
    [
        ("commands", "messunjerr.identity.api.schemas"),  # команды не импортируют api/ (4.4)
        ("queries", "messunjerr.identity.commands.common"),
        ("domain", "messunjerr.identity.infra.models"),  # домен не зависит от инфраструктуры
    ],
)
def test_layers_inside_a_context_cannot_import_upwards(
    project_copy: Path, layer: str, module: str
) -> None:
    (project_copy / "src" / "messunjerr" / "identity" / layer / "violation.py").write_text(
        f"import {module}\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.parametrize(
    ("layer", "module"),
    [
        ("commands", "messunjerr.profiles.api.schemas"),
        ("queries", "messunjerr.profiles.commands.update_profile"),
        ("domain", "messunjerr.profiles.infra.models"),
        ("infra", "messunjerr.profiles.queries.me"),
    ],
)
def test_layers_inside_profiles_cannot_import_upwards(
    project_copy: Path, layer: str, module: str
) -> None:
    (project_copy / "src" / "messunjerr" / "profiles" / layer / "violation.py").write_text(
        f"import {module}\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.parametrize(
    ("layer", "module"),
    [
        ("commands", "messunjerr.media.api.schemas"),
        ("queries", "messunjerr.media.commands.init_upload"),
        ("domain", "messunjerr.media.infra.models"),
        ("infra", "messunjerr.media.queries.assets"),
    ],
)
def test_layers_inside_media_cannot_import_upwards(
    project_copy: Path, layer: str, module: str
) -> None:
    (project_copy / "src" / "messunjerr" / "media" / layer / "violation.py").write_text(
        f"import {module}\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.parametrize("context", ["profiles", "media"])
@pytest.mark.parametrize(
    "module",
    [
        "messunjerr.identity.infra.models",  # модели чужого контекста не импортируем
        "messunjerr.identity.commands.common",
        "messunjerr.identity.queries.accounts",
        "messunjerr.identity.api.deps",  # зависимости берём из api_public, а не напрямую
        "messunjerr.identity.domain.errors",
        "messunjerr.identity.services",
    ],
)
def test_a_higher_context_may_reach_identity_only_through_its_public_interface(
    project_copy: Path, context: str, module: str
) -> None:
    (project_copy / "src" / "messunjerr" / context / "violation.py").write_text(
        f"import {module}\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout
    assert "api_public" in result.stdout


@pytest.mark.parametrize("context", ["profiles", "media"])
def test_the_public_interface_of_identity_is_importable_from_higher_contexts(
    project_copy: Path, context: str
) -> None:
    (project_copy / "src" / "messunjerr" / context / "fine.py").write_text(
        "from messunjerr.identity.api_public import PrincipalDep, find_account\n",
        encoding="utf-8",
    )
    result = _lint_imports(project_copy)
    assert result.returncode == 0, result.stdout + result.stderr


def test_media_may_use_profiles_but_not_the_other_way_round(project_copy: Path) -> None:
    # media стоит выше profiles в графе 4.2: сверху вниз можно (будущее: аватар в карточке).
    (project_copy / "src" / "messunjerr" / "media" / "fine.py").write_text(
        "from messunjerr.profiles import services\n", encoding="utf-8"
    )
    assert _lint_imports(project_copy).returncode == 0

    # Снизу вверх нельзя: profiles знает о файлах только через порт `AssetUsage` (собирает main).
    (project_copy / "src" / "messunjerr" / "media" / "fine.py").unlink()
    (project_copy / "src" / "messunjerr" / "profiles" / "violation.py").write_text(
        "from messunjerr.media import services\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


def test_core_importing_an_entry_point_breaks_the_build(project_copy: Path) -> None:
    (project_copy / "src" / "messunjerr" / "core" / "violation.py").write_text(
        "from messunjerr import main\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


@pytest.mark.parametrize("context", ["core", "identity", "profiles", "media"])
@pytest.mark.parametrize("entry_point", ["seeding", "main", "jobs"])
def test_no_context_may_import_an_entry_point(
    project_copy: Path, context: str, entry_point: str
) -> None:
    (project_copy / "src" / "messunjerr" / context / "violation.py").write_text(
        f"from messunjerr import {entry_point}\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout


def test_identity_cannot_import_profiles_it_uses_ports_instead(project_copy: Path) -> None:
    (project_copy / "src" / "messunjerr" / "identity" / "violation.py").write_text(
        "from messunjerr.profiles import services\n", encoding="utf-8"
    )
    result = _lint_imports(project_copy)
    assert result.returncode != 0
    assert "BROKEN" in result.stdout

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import main
from src.database import engine, get_db
from src.main import app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_python(code, **env):
    """Запускает код в отдельном интерпретаторе: так проверяется поведение при импорте приложения."""
    full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, env=full_env, capture_output=True, text=True, timeout=60
    )


async def test_health_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_reports_unavailable_database(client):
    class BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("database is down")

    async def broken_db():
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_db
    try:
        response = await client.get("/health")
    finally:
        app.dependency_overrides.pop(get_db)
    assert response.status_code == 503


async def test_cors_allows_only_configured_origins(client):
    headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    allowed = await client.options("/posts/", headers={**headers, "Origin": "http://localhost:1337"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:1337"

    foreign = await client.options("/posts/", headers={**headers, "Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in foreign.headers


async def test_openapi_describes_the_api(client):
    spec = (await client.get("/openapi.json")).json()
    assert spec["info"]["title"] == "messunjerr API"
    # варианты путей со слэшем на конце скрыты из документации
    assert "/auth/register" in spec["paths"]
    assert "/auth/register/" not in spec["paths"]
    assert "/auth/users/me/" not in spec["paths"]
    operations = [(method, path) for path, item in spec["paths"].items() for method in item]
    assert len(operations) == 12  # 11 методов API + /health
    register_schema = spec["paths"]["/auth/register"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert register_schema == {"$ref": "#/components/schemas/RegisterResponse"}


async def test_requests_do_not_print_to_stdout(client, make_user, capsys):
    alice = await make_user("alice")
    await client.get("/posts/", headers=alice)
    assert capsys.readouterr().out == ""


class FlakyEngine:
    """Подставной движок: первые `failures` попыток подключения падают, затем работает настоящий."""

    def __init__(self, failures):
        self.failures = failures
        self.attempts = 0

    def begin(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionRefusedError("database is starting")
        return engine.begin()


async def test_init_db_waits_for_database_to_come_up(monkeypatch):
    flaky = FlakyEngine(failures=2)
    monkeypatch.setattr(main, "engine", flaky)
    monkeypatch.setattr(main, "DB_CONNECT_DELAY", 0)
    await main.init_db()
    assert flaky.attempts == 3


async def test_init_db_gives_up_instead_of_swallowing_the_error(monkeypatch):
    flaky = FlakyEngine(failures=100)
    monkeypatch.setattr(main, "engine", flaky)
    monkeypatch.setattr(main, "DB_CONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(main, "DB_CONNECT_DELAY", 0)
    with pytest.raises(ConnectionRefusedError):
        await main.init_db()
    assert flaky.attempts == 3


def test_missing_secret_key_fails_fast():
    result = run_python("import src.config", SECRET_KEY="")
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_database_url_is_built_with_escaped_credentials():
    code = (
        "from sqlalchemy.engine import make_url; import src.config as c;"
        "url = make_url(c.DATABASE_URL); print(url.username, url.password, url.host, url.port, url.database)"
    )
    result = run_python(
        code,
        DATABASE_URL="",
        SECRET_KEY="k",
        POSTGRES_USER="user",
        POSTGRES_PASSWORD="p@ss/w:rd#1",
        POSTGRES_DB="db",
        POSTGRES_HOST="db",
        POSTGRES_PORT="5432",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "user p@ss/w:rd#1 db 5432 db"


def test_importing_the_app_does_not_leak_database_password():
    secret = "sup3r-secret-pw"
    result = run_python(
        "import src.main; from src.database import engine; print('echo =', engine.echo)",
        DATABASE_URL="",
        SECRET_KEY="k",
        POSTGRES_USER="user",
        POSTGRES_PASSWORD=secret,
        POSTGRES_DB="db",
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    # SQL-запросы по умолчанию не пишутся в лог
    assert "echo = False" in result.stdout

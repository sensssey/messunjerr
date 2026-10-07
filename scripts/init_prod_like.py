"""Создаёт секреты prod-подобного стенда в deploy/secrets (только стандартная библиотека).

Запуск: `py -3 scripts/init_prod_like.py [--force]`.

Каждый секрет это отдельный файл: Compose монтирует его в контейнер как /run/secrets/<имя>, а
приложение читает по переменной `<ИМЯ>_FILE` (settings.FileSecretsSource). Файлы в git не попадают.

- Файл уже есть: он не трогается. Повторный запуск безопасен и дописывает только недостающее.
- `--force` создаёт все секреты заново. Осторожно: пароли ролей и ключ шифрования копий нельзя
  сменить под уже созданными томами PostgreSQL и репозиторием pgBackRest. После `--force` стенд
  нужно сбросить: `docker compose -p messunjerr-stand -f deploy/compose.yml down -v` (или
  `make reset-prod-like`); имя проекта `-p messunjerr-stand` обязательно, иначе Compose возьмёт имя
  из окружения и может задеть тома dev-стека.
"""

import base64
import os
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = ROOT / "deploy" / "secrets"

DB_NAME = "messunjerr"


def hex_secret(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def build(existing: dict[str, str]) -> dict[str, str]:
    """Значения секретов; пароли берутся из уже существующих файлов, чтобы адреса сходились."""

    def pick(name: str, nbytes: int = 16) -> str:
        return existing.get(name) or hex_secret(nbytes)

    postgres = pick("postgres_password")
    migrator = pick("db_migrator_password")
    app = pick("db_app_password")
    redis = pick("redis_password")
    seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return {
        "postgres_password": postgres,
        "db_migrator_password": migrator,
        "db_app_password": app,
        "db_readonly_password": pick("db_readonly_password"),
        "redis_password": redis,
        "admin_database_url": f"postgresql+asyncpg://postgres:{postgres}@postgres:5432/postgres",
        "migrator_database_url": f"postgresql+asyncpg://migrator:{migrator}@postgres:5432/{DB_NAME}",
        "database_url": f"postgresql+asyncpg://app:{app}@postgres:5432/{DB_NAME}",
        "redis_url": f"redis://:{redis}@redis:6379/0",
        # Постоянный ключ подписи токенов: seed Ed25519 (как JWT_PRIVATE_KEY в deploy/.env).
        "jwt_private_key": existing.get("jwt_private_key") or seed,
        "smtp_url": "smtp://mailpit:1025",
        # Ключи S3: идентификаторы похожи на настоящие (20 символов), секреты 40 hex-символов.
        "s3_app_access_key": existing.get("s3_app_access_key") or "AK" + hex_secret(9).upper(),
        "s3_app_secret_key": pick("s3_app_secret_key", 20),
        "s3_backup_access_key": existing.get("s3_backup_access_key") or "BK" + hex_secret(9).upper(),
        "s3_backup_secret_key": pick("s3_backup_secret_key", 20),
        # Ключ шифрования копий хранится отдельно от них; потеряв его, копии не прочитать.
        "pgbackrest_cipher_pass": pick("pgbackrest_cipher_pass", 32),
    }


# Файлы, которые монтирует Compose; остальные значения (пароли ролей) нужны только для сборки адресов.
FILES = (
    "postgres_password",
    "admin_database_url",
    "migrator_database_url",
    "database_url",
    "db_readonly_password",
    "redis_password",
    "redis_url",
    "jwt_private_key",
    "smtp_url",
    "s3_app_access_key",
    "s3_app_secret_key",
    "s3_backup_access_key",
    "s3_backup_secret_key",
    "pgbackrest_cipher_pass",
)


def read_existing() -> dict[str, str]:
    """Исходные пароли для сборки адресов: берём из готовых файлов, если они есть."""
    values: dict[str, str] = {}
    for name in FILES:
        path = SECRETS_DIR / name
        if path.exists():
            values[name] = path.read_text(encoding="utf-8").strip()
    # Пароли ролей достаём из адресов, чтобы повторный запуск не менял их.
    for name, role, prefix in (
        ("migrator_database_url", "db_migrator_password", "postgresql+asyncpg://migrator:"),
        ("database_url", "db_app_password", "postgresql+asyncpg://app:"),
    ):
        url = values.get(name)
        if url and url.startswith(prefix):
            values[role] = url[len(prefix) :].split("@", 1)[0]
    return values


def main(argv: list[str]) -> int:
    force = "--force" in argv
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        # Файлы читаются всеми (их монтируют в контейнеры под разными пользователями), поэтому
        # доступ к ним ограничивает каталог. На Windows права NTFS задаёт профиль пользователя.
        SECRETS_DIR.chmod(0o700)
    existing = {} if force else read_existing()
    values = build(existing)

    created: list[str] = []
    for name in FILES:
        path = SECRETS_DIR / name
        if path.exists() and not force:
            continue
        if path.exists():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # на Windows файл «только для чтения» не перезаписать
        path.write_text(values[name] + "\n", encoding="utf-8", newline="\n")
        # Контейнеры читают файл под своими пользователями (appuser, postgres, seaweed), поэтому 0444;
        # доступ ограничивает каталог (на сервере: 0700, владелец root).
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        created.append(name)

    if created:
        print(f"deploy/secrets: создано {len(created)}: {', '.join(created)}")
    else:
        print("deploy/secrets: всё на месте (чтобы создать заново: --force, затем down -v)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

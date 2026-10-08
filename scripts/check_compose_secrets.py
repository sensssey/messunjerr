"""Секреты по сервисам стенда: ключ подписи токенов только у API, пароль SMTP только у воркера почты.

Запуск: `py -3 scripts/check_compose_secrets.py` (нужен Docker Compose; файлы секретов стенда создаёт
`scripts/init_prod_like.py`). Код возврата 1, если секрет смонтирован не тому сервису или переменная
`*_FILE` расходится с секретом (без файла `Settings` падает при старте).

Зачем (S6): воркер `media` разбирает недоверенные файлы кодеками в своём процессе, без песочницы
(спецификация 4.11). Уязвимость кодека дала бы код в этом процессе, поэтому всё, что можно не
монтировать, не монтируется: с ключом подписи токенов злоумышленник выпускал бы токены любого
пользователя и администратора.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Секрет → сервисы, которые вправе его получить.
HOLDERS = {
    "jwt_private_key": {"api-a", "api-b"},
    "smtp_url": {"worker"},
}
# Переменная окружения → секрет, на файл которого она указывает.
FILE_VARIABLES = {
    "JWT_PRIVATE_KEY_FILE": "jwt_private_key",
    "SMTP_URL_FILE": "smtp_url",
}


def compose_config() -> dict[str, Any]:
    output = subprocess.run(
        ["docker", "compose", "-p", "messunjerr-stand", "-f", "deploy/compose.yml", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output)


def secrets_of(service: dict[str, Any]) -> set[str]:
    return {item["source"] if isinstance(item, dict) else item for item in service.get("secrets", [])}


def main() -> int:
    services: dict[str, dict[str, Any]] = compose_config()["services"]
    problems: list[str] = []
    for secret, allowed in HOLDERS.items():
        holders = {name for name, service in services.items() if secret in secrets_of(service)}
        if holders != allowed:
            problems.append(f"секрет {secret} у {sorted(holders)}, ожидалось {sorted(allowed)}")
    for name, service in services.items():
        mounted = secrets_of(service)
        variables = service.get("environment", {})
        for variable, secret in FILE_VARIABLES.items():
            if (variable in variables) != (secret in mounted):
                problems.append(f"{name}: {variable} и секрет {secret} расходятся")
    print("\n".join(problems) if problems else "секреты разведены по сервисам верно")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

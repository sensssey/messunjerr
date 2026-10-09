"""Проверка конфигурации Caddy: журналы не пишут ticket, подписи presigned URL и текст поиска `q`.

Читает из stdin адаптированный конфиг (`caddy adapt --config deploy/Caddyfile`) и проверяет:

- у каждого журнала (доступа, ошибок, служебного) стоит фильтр с заменой секретных параметров адреса
  и удалением Authorization, Cookie и Set-Cookie;
- записи об ошибках запросов (`http.log.error`) идут в журнал с фильтром, а не мимо него.

Запуск (его же делает job `stand-config` в CI):

    docker run --rm -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11-alpine \\
        caddy adapt --config /etc/caddy/Caddyfile | python3 scripts/check_caddy_logging.py

Журнал ошибок содержит запрос целиком (с адресом, который в журнале доступа фильтруется), поэтому
без отдельной проверки утечка вернулась бы незаметно: тесты стенда читают только журнал доступа.
"""

import json
import sys
from typing import Any

SECRET_PARAMETERS = {"ticket", "X-Amz-Signature", "X-Amz-Credential", "X-Amz-Security-Token", "q"}
DELETED_FIELDS = {
    "request>headers>Authorization",
    "request>headers>Cookie",
    "resp_headers>Set-Cookie",
}


def problems_of(name: str, spec: dict[str, Any]) -> list[str]:
    encoder = spec.get("encoder", {})
    if encoder.get("format") != "filter":
        return [f"журнал {name}: нет фильтра, адреса и заголовки пишутся как есть"]
    fields = encoder.get("fields", {})
    found: list[str] = []
    replaced = {
        action.get("parameter")
        for action in fields.get("request>uri", {}).get("actions", [])
        if action.get("type") == "replace"
    }
    if missing := SECRET_PARAMETERS - replaced:
        found.append(f"журнал {name}: параметры адреса без замены: {', '.join(sorted(missing))}")
    if missing_fields := {f for f in DELETED_FIELDS if fields.get(f, {}).get("filter") != "delete"}:
        found.append(f"журнал {name}: заголовки не удаляются: {', '.join(sorted(missing_fields))}")
    return found


def main() -> int:
    config = json.load(sys.stdin)
    logs: dict[str, dict[str, Any]] = config.get("logging", {}).get("logs", {})
    problems: list[str] = []
    for name, spec in logs.items():
        problems += problems_of(name, spec)

    error_logs = [
        name
        for name, spec in logs.items()
        if any(item.startswith("http.log.error") for item in spec.get("include", []))
    ]
    if not error_logs:
        problems.append("ошибки запросов (http.log.error) уходят в журнал по умолчанию, а не в отдельный с фильтром")
    if "default" not in logs:
        problems.append("служебный журнал по умолчанию не настроен: он пишет без фильтра")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"журналы Caddy ({', '.join(sorted(logs))}) скрывают секреты в адресах и заголовках")
    return 0


if __name__ == "__main__":
    sys.exit(main())

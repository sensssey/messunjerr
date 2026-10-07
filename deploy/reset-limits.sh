#!/usr/bin/env bash
# Сбрасывает счётчики лимитов запросов (ключи `rl:*`) в Redis стенда: `make stand-reset-limits`.
#
# Нужен после многократных прогонов тестов стенда: регистраций с одного адреса 5 в час, заявок на
# загрузку файла 60 в час на человека (ratelimits.toml). Очереди задач и сессии не затрагиваются.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)

"${COMPOSE[@]}" exec -T redis sh -c '
  password="$(cat /run/secrets/redis_password)"
  keys="$(redis-cli -a "$password" --no-auth-warning --scan --pattern "rl:*")"
  if [ -z "$keys" ]; then
    echo "счётчиков лимитов нет"
  else
    echo "$keys" | xargs redis-cli -a "$password" --no-auth-warning DEL >/dev/null
    echo "счётчики лимитов сброшены: $(echo "$keys" | wc -l)"
  fi
'

#!/usr/bin/env bash
# Поднимает prod-подобный стенд (S4): секреты, образы, сервисы, корневой сертификат Caddy.
# Это то, что делают `make up-prod-like` и `./dev.ps1 up-prod-like`.
#
# - Секреты создаёт scripts/init_prod_like.py (один раз, существующие не трогает).
# - Если стенд уже обновляли командой deploy, он поднимается из выкаченного тега (deploy/.stand/current_tag),
#   а не из `local`, и образ этого тега не пересобирается: тег неизменяем, а пересборка заменила бы
#   выкаченную версию той, что лежит в рабочем дереве.
# - Копии (pgbackrest) стартуют отдельным шагом без ожидания: их healthcheck красный, если последняя
#   копия старше 26 часов (после выходных или сна ноутбука), и это не должно мешать подъёму стенда.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно, второй набор контейнеров сел бы на те же
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
STATE_DIR="${STATE_DIR:-deploy/.stand}"
mkdir -p "$STATE_DIR"

if command -v py >/dev/null 2>&1; then
  py -3 scripts/init_prod_like.py
else
  python3 scripts/init_prod_like.py
fi

TAG="$(cat "$STATE_DIR/current_tag" 2>/dev/null || true)"
TAG="${TAG:-local}"
export TAG
build=(--build)
if [ "$TAG" != "local" ]; then
  build=(--no-build)
  echo "стенд поднимается из выкаченного тега $TAG (образ не пересобирается)"
fi

"${COMPOSE[@]}" up -d "${build[@]}" --wait caddy api-a api-b worker worker-default worker-media mailpit
"${COMPOSE[@]}" up -d "${build[@]}" pgbackrest
"${COMPOSE[@]}" cp caddy:/data/caddy/pki/authorities/local/root.crt "$STATE_DIR/root.crt" >/dev/null 2>&1

echo "Стенд:   https://messunjerr.localhost/api/v1/meta"
echo "Mailpit: http://localhost:${STAND_MAILPIT_PORT:-8026}"
echo "Сертификат Caddy выгружен в $STATE_DIR/root.crt; чтобы браузер ему доверял: make stand-ca (./dev.ps1 stand-ca)"

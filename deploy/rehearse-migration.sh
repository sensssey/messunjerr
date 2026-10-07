#!/usr/bin/env bash
# Репетиция выкладки с настоящей новой ревизией Alembic (S4): `deploy/rehearse-migration.sh`.
#
# Без новой ревизии нельзя проверить главное для выкладки без простоя: пока migrate продвинул БД,
# старые реплики работают с «чужой» (более новой) схемой и обязаны оставаться готовыми, а откат на
# старый код против новой схемы обязан становиться готовым. Скрипт:
#   1. собирает одноразовый образ с пустой миграцией (SELECT 1: схема не меняется) поверх головы;
#   2. выкатывает его командой rollout.sh deploy (старые реплики видят БД «впереди»);
#   3. откатывает командой rollout.sh rollback (старый код против БД с неизвестной ему ревизией) и
#      показывает ответ /health/ready (migrations: ahead);
#   4. возвращает БД на прежнюю ревизию (alembic downgrade образом с миграцией) и удаляет образ. Если
#      скрипт прервали или выкладка не дошла до отката, сначала приложение возвращается на прежний
#      код (rollout.sh rollback), и только потом понижается БД.
# В обоих шагах фоновая нагрузка rollout.sh считает ошибки клиентов; их быть не должно.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно, второй набор контейнеров сел бы на те же
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
STATE_DIR="${STATE_DIR:-deploy/.stand}"
IMAGE="${BACKEND_IMAGE:-messunjerr-v2-prod}"
BUILD_DIR="$STATE_DIR/rehearsal-build"

log() { printf '\n##### %s\n' "$*"; }

base_tag="$(cat "$STATE_DIR/current_tag" 2>/dev/null || true)"
if [ -z "$base_tag" ]; then
  image="$(docker inspect --format '{{.Config.Image}}' "$("${COMPOSE[@]}" ps -q api-a)")"
  base_tag="${image##*:}"
fi
head="$("${COMPOSE[@]}" exec -T api-a python -c 'from messunjerr.core.migrations import expected_head; print(expected_head())')"
tag="rehearse-$(date +%H%M%S)"
SERVICES=(api-a api-b worker worker-default worker-media)
migrated=0

on_new_code() { # работает ли хоть один сервис приложения из образа репетиции
  local service id
  for service in "${SERVICES[@]}"; do
    id="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    [ -n "$id" ] || continue
    if [ "$(docker inspect --format '{{.Config.Image}}' "$id")" = "$IMAGE:$tag" ]; then return 0; fi
  done
  return 1
}

cleanup() {
  trap - EXIT
  local database_returned=1
  if [ "$migrated" = 1 ]; then
    # БД понижается только после того, как приложение вернулось на прежний код: иначе код с новой
    # ревизией встретил бы схему без неё, а /health/ready стал бы behind.
    if on_new_code; then
      log "приложение ещё на $tag: сначала возвращаем прежний код $base_tag"
      bash deploy/rollout.sh rollback --no-probe || true
      if on_new_code; then
        TAG="$base_tag" "${COMPOSE[@]}" up -d --no-deps --no-build --wait "${SERVICES[@]}" || true
      fi
    fi
    if on_new_code; then
      database_returned=0
      echo "ВНИМАНИЕ: приложение осталось на $tag, БД оставлена на ревизии репетиции. Верните приложение на $base_tag (TAG=$base_tag docker compose -p $PROJECT -f deploy/compose.yml up -d) и выполните alembic downgrade $head" >&2
    else
      log "БД возвращается на ревизию $head"
      TAG="$tag" "${COMPOSE[@]}" run --rm --no-deps -T migrate python -m alembic downgrade "$head" || echo "ВНИМАНИЕ: вернуть БД на $head не удалось, сделайте это вручную" >&2
    fi
  fi
  if [ "$database_returned" = 1 ]; then docker rmi "$IMAGE:$tag" >/dev/null 2>&1 || true; fi
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

log "образ $IMAGE:$tag: $IMAGE:$base_tag плюс пустая миграция поверх $head"
mkdir -p "$BUILD_DIR"
cat >"$BUILD_DIR/9999_rehearsal.py" <<PY
"""rehearsal: пустая миграция для репетиции выкладки (S4), схема не меняется"""

from collections.abc import Sequence

from alembic import op

revision: str = "9999_rehearsal"
down_revision: str | None = "$head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SELECT 1")


def downgrade() -> None:
    op.execute("SELECT 1")
PY
cat >"$BUILD_DIR/Dockerfile" <<DOCKER
FROM $IMAGE:$base_tag
USER root
COPY 9999_rehearsal.py /app/migrations/versions/9999_rehearsal.py
USER appuser
DOCKER
docker build -q -t "$IMAGE:$tag" "$BUILD_DIR" >/dev/null

log "выкладка $tag с новой ревизией (старые реплики увидят БД впереди кода)"
migrated=1
bash deploy/rollout.sh deploy "$tag"

log "откат на $base_tag: старый код против БД с неизвестной ему ревизией"
bash deploy/rollout.sh rollback

log "готовность старого кода при БД впереди"
"${COMPOSE[@]}" exec -T api-a python -c "
import json, urllib.request
body = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready'))
print(body['status'], body['checks'])
assert body['status'] == 'ready' and body['checks']['migrations'] == 'ahead', body
"

echo
echo "РЕПЕТИЦИЯ С МИГРАЦИЕЙ ПРОЙДЕНА: выкладка и откат без ошибок клиентов, готовность при БД впереди кода сохраняется"

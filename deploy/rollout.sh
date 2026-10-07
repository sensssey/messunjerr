#!/usr/bin/env bash
# Выкладка без простоя на prod-подобном стенде (S4-03). В S21 тот же сценарий запускает CD по SSH
# (образ берётся из GHCR командой pull вместо сборки).
#
#   deploy/rollout.sh deploy [ТЕГ] [--no-probe]   собрать образ ТЕГ (по умолчанию метка времени) и выкатить
#   deploy/rollout.sh rollback [--no-probe]       вернуть предыдущую выкладку (или довести/отменить незавершённую)
#   deploy/rollout.sh status                      что выкачено сейчас
#
# Порядок: проверка, что стенд здоров (smoke.sh) → образ → миграции отдельной задачей (migrate) →
# api-a, затем api-b по одной (реплика сливает трафик: /health/serving отдаёт 503, Caddy снимает её с
# балансировки, и только потом она закрывается; перед остановкой проверяется, что соседняя реплика
# готова) → воркеры → дымовой тест. Если после миграций что-то не так, код возвращается на прошлый
# тег; сами миграции назад не откатываются: схема обязана быть совместима с обеими версиями кода
# («расширить → мигрировать → сузить», 4.16), а готовность реплики не страдает от БД, которая
# «впереди» кода (/health/ready: migrations=ahead).
#
# Состояние лежит в deploy/.stand:
#   current_tag   что выкачено;
#   previous_tag  куда вернёт rollback (после успешного отката пусто: повторный rollback не катит
#                 обратно на только что отвергнутую версию, нужную версию называют явно: deploy ТЕГ);
#   pending       "ВИД ОТКУДА КУДА" (ВИД: deploy или rollback), пока операция идёт.
# Если операцию прервали, pending остаётся. Прерванную выкладку завершает `deploy <тот же тег>`
# или отменяет `rollback` (вернёт ОТКУДА). Прерванный откат доводит до конца `rollback` (катит КУДА);
# выкладка при таком состоянии отказывается стартовать. Тег образа неизменяем: повторная выкладка
# уже выкаченного тега ничего не делает.
#
# Всё время операции фоновая нагрузка (tests_v2/stand/probe.py: запросы, SSE, WebSocket) считает
# ошибки клиентов; результат считается чистым, только если их нет.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Имя проекта не настраивается: тома стенда названы явно (messunjerr-stand_pgdata и т. д.), и второй
# набор контейнеров под другим именем сел бы на те же тома.
PROJECT="messunjerr-stand"
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
STATE_DIR="${STATE_DIR:-deploy/.stand}"
IMAGE="${BACKEND_IMAGE:-messunjerr-v2-prod}"
READY_TIMEOUT="${READY_TIMEOUT:-90}"
# Caddy включает реплику после первой успешной проверки (health_interval 2 с).
CADDY_SETTLE="${CADDY_SETTLE:-4}"
PROBE_NAME="$PROJECT-probe"
REPLICAS=(api-a api-b)
WORKERS=(worker worker-default worker-media)
mkdir -p "$STATE_DIR"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'rollout: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'TEXT'
deploy/rollout.sh deploy [ТЕГ] [--no-probe]   собрать образ ТЕГ и выкатить без простоя
deploy/rollout.sh rollback [--no-probe]       вернуть предыдущую выкладку (или довести/отменить незавершённую)
deploy/rollout.sh status                      что выкачено сейчас
Порядок, состояние и откат описаны в заголовке этого файла и в docs/runbooks/stand.md.
TEXT
}

# --------------------------------------------------------------------------- состояние
state_get() { cat "$STATE_DIR/$1" 2>/dev/null || true; }
state_set() { printf '%s' "$2" >"$STATE_DIR/$1"; }
state_clear() { rm -f "$STATE_DIR/$1"; }

PENDING_KIND="" PENDING_FROM="" PENDING_TO=""
read_pending() { # заполняет PENDING_*; пусто, если операции в процессе нет
  PENDING_KIND="" PENDING_FROM="" PENDING_TO=""
  local raw words
  raw="$(state_get pending)"
  [ -n "$raw" ] || return 0
  read -r -a words <<<"$raw"
  case "${#words[@]}" in
    3) PENDING_KIND="${words[0]}" PENDING_FROM="${words[1]}" PENDING_TO="${words[2]}" ;;
    2) PENDING_KIND="deploy" PENDING_FROM="${words[0]}" PENDING_TO="${words[1]}" ;; # прежний формат
    *) return 1 ;;
  esac
  case "$PENDING_KIND" in deploy | rollback) ;; *) return 1 ;; esac
}

replica_tag() { # тег образа, из которого запущена реплика
  local id image
  id="$("${COMPOSE[@]}" ps -q "$1" 2>/dev/null || true)"
  [ -n "$id" ] || return 1
  image="$(docker inspect --format '{{.Config.Image}}' "$id")"
  printf '%s' "${image##*:}"
}

running_tag() { # общий тег обеих реплик; код 1, если их нет или они из разных сборок
  local first second
  first="$(replica_tag "${REPLICAS[0]}")" || return 1
  second="$(replica_tag "${REPLICAS[1]}")" || return 1
  [ "$first" = "$second" ] || return 1
  printf '%s' "$first"
}

replica_build() { # сборка, из которой отвечает реплика (поле build в /api/v1/meta)
  "${COMPOSE[@]}" exec -T "$1" python -c \
    "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/meta'))['build'])"
}

replica_ready() {
  "${COMPOSE[@]}" exec -T "$1" python -m messunjerr healthcheck \
    --url http://127.0.0.1:8000/health/ready >/dev/null 2>&1
}

wait_ready() {
  local service="$1" deadline=$((SECONDS + READY_TIMEOUT))
  until replica_ready "$service"; do
    [ "$SECONDS" -lt "$deadline" ] || { echo "rollout: $service не стал готовым за ${READY_TIMEOUT} с" >&2; return 1; }
    sleep 1
  done
}

# --------------------------------------------------------------------------- шаги
ensure_image() {
  local tag="$1"
  if docker image inspect "$IMAGE:$tag" >/dev/null 2>&1; then
    echo "образ $IMAGE:$tag уже есть"
    return 0
  fi
  log "сборка образа $IMAGE:$tag"
  TAG="$tag" "${COMPOSE[@]}" build api-a
}

run_migrations() {
  local tag="$1"
  log "миграции (отдельная задача, образ $tag)"
  TAG="$tag" "${COMPOSE[@]}" run --rm --no-deps -T migrate
}

roll_replica() {
  local service="$1" tag="$2" strict="$3" other build
  # Пока одна реплика перезапускается, весь трафик на соседней: при выкладке она должна быть готова
  # сейчас. При откате этого не требуем: он нужен именно тогда, когда что-то уже сломано.
  if [ "$strict" = 1 ]; then
    for other in "${REPLICAS[@]}"; do
      if [ "$other" != "$service" ] && ! replica_ready "$other"; then
        echo "rollout: $other не готова, останавливать $service нельзя (это был бы простой)" >&2
        return 1
      fi
    done
  fi
  log "$service → $tag"
  TAG="$tag" "${COMPOSE[@]}" up -d --no-deps --no-build --wait "$service" || return 1
  wait_ready "$service" || return 1
  build="$(replica_build "$service")" || return 1
  if [ "$build" != "$tag" ]; then
    echo "rollout: $service отвечает сборкой $build, ожидалась $tag" >&2
    return 1
  fi
  sleep "$CADDY_SETTLE"
}

# roll_all ТЕГ СТРОГО(1|0): при выкладке (1) соседняя реплика должна быть готова; при откате (0) сначала
# возвращаем неготовые реплики: именно они, скорее всего, и сломаны.
roll_all() {
  local tag="$1" strict="$2" service ordered=()
  for service in "${REPLICAS[@]}"; do
    if [ "$strict" = 0 ] && ! replica_ready "$service"; then ordered=("$service" "${ordered[@]}"); else ordered+=("$service"); fi
  done
  for service in "${ordered[@]}"; do
    roll_replica "$service" "$tag" "$strict" || return 1
  done
  log "воркеры → $tag"
  TAG="$tag" "${COMPOSE[@]}" up -d --no-deps --no-build --wait "${WORKERS[@]}" || return 1
  bash deploy/smoke.sh "$tag"
}

# --------------------------------------------------------------------------- фоновая нагрузка
PROBE_ON=1
probe_started=0

start_probe() {
  docker rm -f "$PROBE_NAME" >/dev/null 2>&1 || true
  log "фоновая нагрузка: запросы, SSE и WebSocket через Caddy"
  "${COMPOSE[@]}" run -d --no-deps --name "$PROBE_NAME" stand-tools python tests_v2/stand/probe.py >/dev/null
  probe_started=1
  sleep 4 # дать нагрузке набрать ход до первого перезапуска
}

stop_probe() { # печатает итог нагрузки; код возврата 0, если ошибок клиентов не было
  [ "$probe_started" = 1 ] || return 0
  probe_started=0
  sleep 3
  log "итог фоновой нагрузки"
  docker stop -t 20 "$PROBE_NAME" >/dev/null
  docker logs "$PROBE_NAME" 2>&1 | grep -v "^probe: нагрузка идёт" || true
  local code
  code="$(docker inspect --format '{{.State.ExitCode}}' "$PROBE_NAME")"
  docker rm "$PROBE_NAME" >/dev/null
  return "$code"
}

cleanup() {
  if [ "$probe_started" = 1 ]; then
    docker rm -f "$PROBE_NAME" >/dev/null 2>&1 || true
  fi
}
interrupted() {
  cleanup
  read_pending 2>/dev/null || true
  echo >&2
  case "$PENDING_KIND" in
    deploy)
      echo "rollout: прервано посреди выкладки ($PENDING_FROM → $PENDING_TO): реплики могут работать из разных сборок. Завершить: deploy $PENDING_TO; отменить: rollback (вернёт $PENDING_FROM)." >&2 ;;
    rollback)
      echo "rollout: прервано посреди отката ($PENDING_FROM → $PENDING_TO): реплики могут работать из разных сборок. Довести до конца: rollback (вернёт $PENDING_TO)." >&2 ;;
    *)
      echo "rollout: прервано." >&2 ;;
  esac
  exit 130
}
trap cleanup EXIT
trap interrupted INT TERM

# --------------------------------------------------------------------------- команды
cmd_status() {
  local service
  echo "выкачено: $(state_get current_tag || true) (предыдущая: $(state_get previous_tag || true))"
  if ! read_pending; then
    echo "ФАЙЛ СОСТОЯНИЯ pending ПОВРЕЖДЁН: $(state_get pending) (удалите $STATE_DIR/pending и сверьте теги реплик ниже)"
  elif [ -n "$PENDING_KIND" ]; then
    case "$PENDING_KIND" in
      deploy) echo "НЕЗАВЕРШЁННАЯ выкладка: $PENDING_FROM → $PENDING_TO (завершить: deploy $PENDING_TO; отменить: rollback)" ;;
      rollback) echo "НЕЗАВЕРШЁННЫЙ откат: $PENDING_FROM → $PENDING_TO (довести до конца: rollback)" ;;
    esac
  fi
  for service in "${REPLICAS[@]}"; do
    echo "$service: образ $(replica_tag "$service" || echo '—'), сборка $(replica_build "$service" 2>/dev/null || echo 'не отвечает')"
  done
}

# deploy_to ВИД ТЕГ ОТКУДА: ВИД deploy (миграции, соседка готова, при сбое возврат на ОТКУДА) или rollback.
deploy_to() {
  local kind="$1" tag="$2" previous="$3" migrate=0 rolled=1 stuck=0 probe_ok=0
  if [ "$kind" = deploy ]; then migrate=1; fi
  if [ "$PROBE_ON" = 1 ]; then start_probe; fi

  if [ "$migrate" = 1 ] && ! run_migrations "$tag"; then
    stop_probe || true
    die "миграции не прошли: реплики не тронуты, работает $previous"
  fi

  state_set pending "$kind $previous $tag"
  if roll_all "$tag" "$migrate"; then
    if [ "$kind" = deploy ]; then
      state_set previous_tag "$previous"
    else
      state_clear previous_tag # вернулись с отвергнутой версии: повторный rollback не должен катить на неё
    fi
    state_set current_tag "$tag"
    state_clear pending
  else
    rolled=0
    if [ "$kind" = deploy ]; then
      log "СБОЙ выкладки $tag: возвращаю $previous"
      state_set pending "rollback $tag $previous"
      if roll_all "$previous" 0; then
        state_set current_tag "$previous"
        state_clear pending
      else
        stuck=1
        echo "rollout: откат на $previous тоже не удался, нужно вмешательство: после починки rollback доведёт возврат на $previous" >&2
      fi
    else
      stuck=1
      echo "rollout: откат на $tag не удался, состояние сохранено: устраните причину и повторите rollback" >&2
    fi
  fi

  stop_probe || probe_ok=1
  if [ "$rolled" = 1 ] && [ "$probe_ok" = 0 ]; then
    log "ГОТОВО: выкачено $tag (предыдущая: $previous)"
    return 0
  fi
  if [ "$rolled" = 1 ]; then
    log "$(printf '%s' "$kind" | tr 'a-z' 'A-Z') $tag ПРОШЁЛ, НО КЛИЕНТЫ ВИДЕЛИ ОШИБКИ (см. итог нагрузки выше)"
  elif [ "$stuck" = 1 ]; then
    log "$(printf '%s' "$kind" | tr 'a-z' 'A-Z') $tag НЕ УДАЛСЯ И ВОЗВРАТ НЕ УДАЛСЯ: стенд в промежуточном состоянии (rollout.sh status)"
  else
    log "ВЫКЛАДКА $tag НЕ УДАЛАСЬ: работает $previous"
  fi
  return 1
}

preflight() {
  log "проверка стенда перед выкладкой"
  bash deploy/smoke.sh >/dev/null || { bash deploy/smoke.sh; die "стенд не в порядке: выкладка при неготовой реплике дала бы простой"; }
  echo "стенд готов"
}

cmd_deploy() {
  local tag="" previous running=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-probe) PROBE_ON=0 ;;
      -*) die "неизвестный флаг $1" ;;
      *) tag="$1" ;;
    esac
    shift
  done
  [ -n "$tag" ] || tag="$(date +%Y%m%d-%H%M%S)"
  case "$tag" in *[!A-Za-z0-9._-]*) die "в теге допустимы буквы, цифры, точка, дефис и подчёркивание" ;; esac

  read_pending || die "файл состояния $STATE_DIR/pending повреждён ($(state_get pending)): удалите его и проверьте rollout.sh status"
  case "$PENDING_KIND" in
    rollback)
      die "не завершён откат $PENDING_FROM → $PENDING_TO: доведите его командой rollback (вернёт $PENDING_TO), затем выкатывайте" ;;
    deploy)
      [ "$tag" = "$PENDING_TO" ] \
        || die "не завершена выкладка $PENDING_FROM → $PENDING_TO: завершите её (deploy $PENDING_TO) или отмените (rollback, вернёт $PENDING_FROM)"
      previous="$PENDING_FROM" ;;
    *)
      running="$(running_tag || true)"
      # Правда о выкаченном это то, из чего запущены реплики; состояние может отстать (ручной `up`).
      previous="${running:-$(state_get current_tag)}"
      [ -n "$previous" ] || previous="$(replica_tag "${REPLICAS[0]}" || true)"
      [ -n "$previous" ] || die "стенд не запущен: ./dev.ps1 up-prod-like"
      if [ -n "$running" ] && [ "$tag" = "$running" ]; then
        state_set current_tag "$running"
        log "тег $tag уже выкачен: тег образа неизменяем, выкатывать нечего (нужна новая версия: другой тег)"
        return 0
      fi
      if [ -z "$running" ]; then
        echo "rollout: реплики запущены из разных сборок, выкладка $tag выровняет их (откат вернёт $previous)" >&2
      fi ;;
  esac

  preflight
  log "выкладка $previous → $tag"
  ensure_image "$tag"
  deploy_to deploy "$tag" "$previous"
}

cmd_rollback() {
  local target current
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-probe) PROBE_ON=0 ;;
      *) die "неизвестный аргумент $1" ;;
    esac
    shift
  done
  read_pending || die "файл состояния $STATE_DIR/pending повреждён ($(state_get pending)): удалите его и проверьте rollout.sh status"
  case "$PENDING_KIND" in
    deploy)   target="$PENDING_FROM" current="$PENDING_TO" ;;   # отмена прерванной выкладки
    rollback) target="$PENDING_TO" current="$PENDING_FROM" ;;   # прерванный откат доводится до конца
    *)
      target="$(state_get previous_tag)"
      current="$(state_get current_tag)"
      [ -n "$target" ] || die "нет сохранённой предыдущей выкладки (откатываться некуда; нужную версию можно выкатить явно: deploy ТЕГ)" ;;
  esac
  docker image inspect "$IMAGE:$target" >/dev/null 2>&1 || die "образа $IMAGE:$target уже нет"
  log "откат ${current:-?} → $target (миграции не откатываются)"
  deploy_to rollback "$target" "${current:-$target}"
}

case "${1:-}" in
  deploy) shift; cmd_deploy "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  status) cmd_status ;;
  *) usage; exit 1 ;;
esac

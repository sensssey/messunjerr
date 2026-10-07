#!/usr/bin/env bash
# Учение по восстановлению из резервной копии на ОТДЕЛЬНОЙ БД (S4-06): `deploy/restore-drill.sh`.
# Боевой PostgreSQL и репозиторий копий не затрагиваются; результат и время (RTO) печатаются в конце.
#
# Что происходит:
#   1. в боевой базе backup_drill появляется метка: она записана ПОСЛЕ последней копии, значит в
#      восстановленной БД окажется только если сработает архивация WAL (RPO);
#   2. запоминаются ревизия миграций и число строк по всем таблицам приложения;
#   3. `pgbackrest check` переключает WAL и ждёт, пока сегмент с меткой уйдёт в хранилище;
#   4. контейнер restore-drill восстанавливает копию в свободный том pgrestore, поднимает кластер
#      с выключенной архивацией, доигрывает WAL и сверяет метку, миграции и число строк.
# Если в боевую БД писали во время учения (воркер, запросы), число строк может разойтись само по себе:
# скрипт сверяет его ещё раз и тогда не считает это ошибкой. Восстановленная копия из тома pgrestore
# после учения удаляется (DRILL_KEEP=1 оставляет её для разбора).
# Перед боевым восстановлением и после него читайте docs/runbooks/backup-restore.md.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно, второй набор контейнеров сел бы на те же
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
DB_NAME="${DB_NAME:-messunjerr}"

psql_live() { "${COMPOSE[@]}" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -qtAX "$@"; }
log() { printf '\n==> %s\n' "$*"; }

log "есть ли копия"
"${COMPOSE[@]}" exec -T pgbackrest /bin/sh /backup/status.sh || {
  echo "restore-drill: сначала нужна копия: ./dev.ps1 backup full" >&2
  exit 1
}

log "метка в боевой БД (после последней копии)"
marker="drill-$(date +%Y%m%d%H%M%S)-$RANDOM"
if ! psql_live -d postgres -c "SELECT 1 FROM pg_database WHERE datname = 'backup_drill'" | grep -q 1; then
  psql_live -d postgres -c "CREATE DATABASE backup_drill"
fi
psql_live -d backup_drill -c "CREATE TABLE IF NOT EXISTS marker (value text PRIMARY KEY, written_at timestamptz NOT NULL DEFAULT now())"
psql_live -d backup_drill -c "INSERT INTO marker (value) VALUES ('$marker')"
echo "метка: $marker"

expect_alembic="$(psql_live -d "$DB_NAME" -c 'SELECT version_num FROM alembic_version')"
expect_counts="$(psql_live -d "$DB_NAME" -f /backup/row-counts.sql)"
echo "миграции: $expect_alembic, таблиц: $(printf '%s\n' "$expect_counts" | wc -l | tr -d ' ')"

log "WAL с меткой уходит в хранилище (pgbackrest check)"
"${COMPOSE[@]}" exec -T pgbackrest /bin/sh -c '. /backup/lib.sh && pgbackrest check'

log "восстановление на отдельной БД (контейнер restore-drill, том pgrestore)"
code=0
DRILL_MARKER="$marker" EXPECT_ALEMBIC="$expect_alembic" EXPECT_COUNTS="$expect_counts" \
  "${COMPOSE[@]}" run --rm -T restore-drill || code=$?

# Код 3: метка и миграции в порядке, а число строк не совпало. Если за время учения в боевую БД
# что-то писали (воркер, запросы), расхождение ожидаемо; если нет, это настоящая ошибка.
if [ "$code" = 3 ]; then
  after="$(psql_live -d "$DB_NAME" -f /backup/row-counts.sql)"
  if [ "$after" != "$expect_counts" ]; then
    log "в боевую БД писали во время учения: расхождение числа строк ожидаемо. Метка (WAL) и миграции подтверждены; для строгой сверки повторите учение на тихой системе"
    exit 0
  fi
  log "боевая БД не менялась, а число строк в восстановленной другое: УЧЕНИЕ НЕ ПРОЙДЕНО"
  exit 1
fi
exit "$code"

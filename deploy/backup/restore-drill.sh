#!/bin/sh
# Учение по восстановлению (S4-06), часть внутри контейнера restore-drill: копия поднимается на
# ОТДЕЛЬНОМ каталоге данных (том pgrestore), а не в боевой БД, и проверяется. Боевой PostgreSQL и
# репозиторий копий не затрагиваются: у восстановленного кластера выключена архивация, иначе его
# новая ветка времени попала бы в боевой репозиторий.
#
# Запускать через deploy/restore-drill.sh (`./dev.ps1 restore-drill`): он пишет в боевую БД метку,
# которой нет в последней копии (её можно получить только из WAL), и передаёт ожидаемые значения:
#   DRILL_MARKER     значение метки в базе backup_drill
#   EXPECT_ALEMBIC   ревизия миграций боевой БД
#   EXPECT_COUNTS    число строк по таблицам боевой БД на момент метки (выборка backup/row-counts.sql)
#   DRILL_KEEP=1     не удалять восстановленный кластер после учения (для разбора)
#
# Код возврата: 0 учение пройдено; 1 ошибка (метка, миграции, восстановление); 3 метка и миграции в
# порядке, но число строк не совпало: если в боевую БД шла запись, это ожидаемо (решает скрипт хоста).
# Восстановленная копия удаляется из тома pgrestore в конце: в ней данные сверх срока хранения.
set -eu
. /backup/lib.sh

PGDATA=/var/lib/postgresql/18/docker
PORT=5433
APP_DB="${DB_NAME:-messunjerr}"

log() { printf 'drill: %s\n' "$*"; }
fail() { log "ОШИБКА: $*"; exit 1; }
psql_q() { psql -h /tmp -p "$PORT" -U postgres -d "$1" -v ON_ERROR_STOP=1 -qtAX -c "$2"; }

started="$(date +%s)"

log "очищаю каталог данных восстановления"
rm -rf "${PGDATA:?}"
mkdir -p "$PGDATA"
chmod 700 "$PGDATA"

log "восстанавливаю из репозитория (pgbackrest restore)"
pgbackrest --pg1-path="$PGDATA" --archive-mode=off restore
restored="$(date +%s)"

log "запускаю восстановленный кластер на порту $PORT, архивация выключена"
pg_ctl -D "$PGDATA" -w -t 900 -l /tmp/drill-postgres.log \
  -o "-p $PORT -c listen_addresses='' -c unix_socket_directories=/tmp -c archive_mode=off -c archive_command=/bin/true" \
  start >/dev/null

tries=0
until [ "$(psql_q postgres 'SELECT pg_is_in_recovery()' 2>/dev/null || echo t)" = "f" ]; do
  tries=$((tries + 1))
  [ "$tries" -lt 600 ] || fail "кластер не вышел из восстановления за 10 минут (см. /tmp/drill-postgres.log)"
  sleep 1
done
recovered="$(date +%s)"
log "кластер восстановлен и принял запись (ветка времени $(psql_q postgres 'SELECT timeline_id FROM pg_control_checkpoint()'))"

status=0
counts_differ=0
version="$(psql_q "$APP_DB" 'SELECT version_num FROM alembic_version')"
log "миграции на ревизии $version"
if [ -n "${EXPECT_ALEMBIC:-}" ] && [ "$version" != "$EXPECT_ALEMBIC" ]; then
  log "ОШИБКА: ожидалась ревизия $EXPECT_ALEMBIC"
  status=1
fi
counts="$(psql -h /tmp -p "$PORT" -U postgres -d "$APP_DB" -v ON_ERROR_STOP=1 -qtAX -f /backup/row-counts.sql)"
tables="$(printf '%s\n' "$counts" | wc -l | tr -d ' ')"
if [ -n "${EXPECT_COUNTS:-}" ]; then
  if [ "$counts" = "$EXPECT_COUNTS" ]; then
    log "число строк во всех $tables таблицах совпадает с боевой БД"
  else
    log "ОШИБКА: число строк расходится с боевой БД (ожидалось, получено):"
    printf '%s\n' "$EXPECT_COUNTS" >/tmp/expected-counts.txt
    printf '%s\n' "$counts" >/tmp/restored-counts.txt
    diff /tmp/expected-counts.txt /tmp/restored-counts.txt || true
    counts_differ=1
  fi
fi
if [ -n "${DRILL_MARKER:-}" ]; then
  found="$(psql_q backup_drill "SELECT count(*) FROM marker WHERE value = '$DRILL_MARKER'")"
  if [ "$found" = "1" ]; then
    log "метка $DRILL_MARKER найдена: WAL после последней копии применён"
  else
    log "ОШИБКА: метки $DRILL_MARKER нет в восстановленной базе"
    status=1
  fi
fi
log "целостность: $(psql_q "$APP_DB" "SELECT count(*) FROM pg_class WHERE relkind = 'r'") таблиц, проверка контрольных сумм страниц включена: $(psql_q postgres 'SHOW data_checksums')"

pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
finished="$(date +%s)"
log "время: восстановление файлов $((restored - started)) с, запуск и доигрывание WAL $((recovered - restored)) с, всего $((finished - started)) с (цель RTO ≤ 1 часа)"
if [ "${DRILL_KEEP:-}" != "1" ]; then
  rm -rf "${PGDATA:?}"
  log "восстановленный кластер удалён из тома (DRILL_KEEP=1 оставляет его для разбора)"
fi
if [ "$status" -ne 0 ]; then
  log "УЧЕНИЕ НЕ ПРОЙДЕНО"
  exit 1
fi
if [ "$counts_differ" -ne 0 ]; then
  log "метка и миграции в порядке, число строк расходится"
  exit 3
fi
log "УЧЕНИЕ ПРОЙДЕНО"

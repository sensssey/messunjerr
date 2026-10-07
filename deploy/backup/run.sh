#!/bin/sh
# Планировщик резервных копий (S4-06). Работает рядом с PostgreSQL: тот же образ, общий каталог
# данных (только чтение) и сокет. Порядок:
#   1. ждёт PostgreSQL, создаёт stanza и проверяет архивацию WAL;
#   2. если копий ещё нет, сразу делает полную;
#   3. дальше раз в сутки в BACKUP_AT (UTC): полная в день BACKUP_FULL_DOW (1..7, 7 = воскресенье),
#      в остальные дни дифференциальная; старые копии стирает expire после каждой копии.
# WAL уходит в хранилище непрерывно: это делает archive_command самого PostgreSQL (RPO = archive_timeout).
set -eu
. /backup/lib.sh

BACKUP_AT="${BACKUP_AT:-03:00}"
BACKUP_FULL_DOW="${BACKUP_FULL_DOW:-7}"
RETRY_SECONDS="${BACKUP_RETRY_SECONDS:-600}"

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

sleeper=""
trap 'log "остановка"; [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null; exit 0' TERM INT

# Сон, который прерывается сигналом (обычный `sleep` задержал бы остановку контейнера).
nap() {
  sleep "$1" &
  sleeper=$!
  wait "$sleeper" || true
  sleeper=""
}

run_backup() {
  log "копия ($1): начало"
  if pgbackrest --type="$1" backup; then
    log "копия ($1): готова"
  else
    log "копия ($1): ошибка"
    return 1
  fi
}

until pg_isready -h /var/run/postgresql -U postgres -q; do nap 2; done
log "PostgreSQL отвечает"

until pgbackrest stanza-create; do
  log "stanza-create не удалась, повтор через ${RETRY_SECONDS} с"
  nap "$RETRY_SECONDS"
done
until pgbackrest check; do
  log "check не прошёл, повтор через ${RETRY_SECONDS} с"
  nap "$RETRY_SECONDS"
done

if pgbackrest --output=json info | grep -q '"backup":\[\]'; then
  log "копий ещё нет: первая полная"
  until run_backup full; do nap "$RETRY_SECONDS"; done
fi

while true; do
  now="$(date +%s)"
  target="$(date -d "today $BACKUP_AT" +%s)"
  [ "$target" -gt "$now" ] || target=$((target + 86400))
  log "следующая копия через $((target - now)) с (в $BACKUP_AT UTC)"
  nap $((target - now))
  kind="diff"
  [ "$(date +%u)" = "$BACKUP_FULL_DOW" ] && kind="full"
  until run_backup "$kind"; do nap "$RETRY_SECONDS"; done
done

#!/bin/sh
# Состояние копий: `pgbackrest info` и возраст последней. Код 1, если копий нет или последняя старше
# BACKUP_MAX_AGE_HOURS (по умолчанию 26 часов: порог оповещения из 4.15). `--quiet` не печатает info.
set -eu
. /backup/lib.sh

MAX_HOURS="${BACKUP_MAX_AGE_HOURS:-26}"
[ "${1:-}" = "--quiet" ] || pgbackrest info

json="$(pgbackrest --output=json info)"
last="$(printf '%s' "$json" | grep -o '"stop":[0-9]*' | cut -d: -f2 | sort -n | tail -1)"
if [ -z "$last" ]; then
  echo "резервных копий нет" >&2
  exit 1
fi
age=$(($(date +%s) - last))
echo "последняя копия завершена $((age / 3600)) ч $((age % 3600 / 60)) мин назад (порог ${MAX_HOURS} ч)"
[ "$age" -le $((MAX_HOURS * 3600)) ]

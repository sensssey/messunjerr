#!/bin/sh
# Копия по требованию: `deploy/backup/now.sh [full|diff|incr]` внутри контейнера pgbackrest
# (снаружи: `./dev.ps1 backup [full|diff|incr]`).
set -eu
. /backup/lib.sh
exec pgbackrest --type="${1:-diff}" backup

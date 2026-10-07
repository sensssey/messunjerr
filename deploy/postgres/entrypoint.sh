#!/bin/sh
# Обёртка над docker-entrypoint.sh PostgreSQL: перед запуском сервера секреты pgBackRest попадают в
# окружение, откуда их берёт archive_command (см. backup/lib.sh).
set -eu
. /backup/lib.sh
exec docker-entrypoint.sh "$@"

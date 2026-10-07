# shellcheck shell=sh
# Общие настройки pgBackRest для контейнеров postgres, pgbackrest и restore-drill.
# Подключается командой `. /backup/lib.sh`. У pgBackRest нет вариантов `*_FILE`, поэтому секреты
# из файлов Compose читаются в переменные окружения (их видят и archive_command, и backup).
# Ключи S3 те же, что у удостоверения `backup` в SeaweedFS (deploy/seaweedfs/entrypoint.sh).
export PGBACKREST_CONFIG=/backup/pgbackrest.conf
export PGBACKREST_STANZA=messunjerr
PGBACKREST_REPO1_S3_KEY="$(tr -d '\r\n' < /run/secrets/s3_backup_access_key)"
PGBACKREST_REPO1_S3_KEY_SECRET="$(tr -d '\r\n' < /run/secrets/s3_backup_secret_key)"
PGBACKREST_REPO1_CIPHER_PASS="$(tr -d '\r\n' < /run/secrets/pgbackrest_cipher_pass)"
export PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET PGBACKREST_REPO1_CIPHER_PASS

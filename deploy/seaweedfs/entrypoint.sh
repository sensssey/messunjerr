#!/bin/sh
# Обёртка над entrypoint SeaweedFS (образ из deploy/seaweedfs/Dockerfile):
#
# 1. собирает конфиг удостоверений S3 из ключей, которые приходят переменными (разработка) или
#    файлами `<ИМЯ>_FILE` (стенд, секреты Compose); ключей нет ни в образе, ни в git;
# 2. выставляет наружу только S3: пробрасывает 8333 (HTTP) и, если задано S3_FORWARD_HTTPS, 8334
#    (HTTPS) на порты SeaweedFS, которые слушают 127.0.0.1 (почему так: Dockerfile рядом);
# 3. в фоне создаёт buckets из S3_BUCKETS, когда master и filer ответят (повтор безопасен).
#
# Удостоверения:
#   app        API и воркер: bucket media (обязательно: S3_APP_ACCESS_KEY, S3_APP_SECRET_KEY);
#   backup     pgBackRest: bucket pgbackrest (только если заданы S3_BACKUP_ACCESS_KEY и S3_BACKUP_SECRET_KEY);
#   anonymous  без подписи разрешено лишь чтение media/public/* (аватары).
set -eu

# Порты, на которых SeaweedFS слушает loopback (должны совпасть с флагами -s3.port и -s3.port.https).
S3_LOCAL_HTTP="${S3_LOCAL_HTTP:-9000}"
S3_LOCAL_HTTPS="${S3_LOCAL_HTTPS:-9001}"

# Значение ключа из переменной ИМЯ или из файла ИМЯ_FILE; пусто, если не задано ни то, ни другое.
read_key() {
  file="$(printenv "${1}_FILE" || true)"
  if [ -n "$file" ]; then
    tr -d '\r\n' < "$file"
  else
    printf '%s' "$(printenv "$1" || true)"
  fi
}

APP_KEY="$(read_key S3_APP_ACCESS_KEY)"
APP_SECRET="$(read_key S3_APP_SECRET_KEY)"
BACKUP_KEY="$(read_key S3_BACKUP_ACCESS_KEY)"
BACKUP_SECRET="$(read_key S3_BACKUP_SECRET_KEY)"

if [ -z "$APP_KEY" ] || [ -z "$APP_SECRET" ]; then
  echo "seaweedfs: не заданы S3_APP_ACCESS_KEY и S3_APP_SECRET_KEY (или их *_FILE)" >&2
  exit 1
fi

BACKUP_IDENTITY=""
if [ -n "$BACKUP_KEY" ] && [ -n "$BACKUP_SECRET" ]; then
  BACKUP_IDENTITY=$(cat <<JSON
    {
      "name": "backup",
      "credentials": [{ "accessKey": "${BACKUP_KEY}", "secretKey": "${BACKUP_SECRET}" }],
      "actions": ["Admin:pgbackrest", "Read:pgbackrest", "Write:pgbackrest", "List:pgbackrest"]
    },
JSON
)
fi

# Конфиг читает только пользователь seaweed, под которого переключается оригинальный entrypoint.
CONFIG=/tmp/s3.json
umask 077
cat > "$CONFIG" <<JSON
{
  "identities": [
    {
      "name": "app",
      "credentials": [{ "accessKey": "${APP_KEY}", "secretKey": "${APP_SECRET}" }],
      "actions": ["Read:media", "Write:media", "List:media", "Tagging:media"]
    },
${BACKUP_IDENTITY}
    {
      "name": "anonymous",
      "actions": ["Read:media/public/*"]
    }
  ]
}
JSON
chown seaweed "$CONFIG"

# Сквозной проброс: пока SeaweedFS поднимается, соединение просто не устанавливается.
forward() {
  (
    while :; do
      su-exec seaweed socat "TCP-LISTEN:$1,fork,reuseaddr" "TCP:127.0.0.1:$2" || true
      sleep 1
    done
  ) &
}
forward 8333 "$S3_LOCAL_HTTP"
if [ -n "${S3_FORWARD_HTTPS:-}" ]; then
  forward 8334 "$S3_LOCAL_HTTPS"
fi

# Buckets создаёт тот же контейнер: master слушает только loopback и снаружи недоступен.
if [ -n "${S3_BUCKETS:-}" ]; then
  /bin/sh /seaweedfs-buckets.sh &
fi

exec /entrypoint.sh "$@" -s3.config="$CONFIG"

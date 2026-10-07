#!/bin/sh
# Одноразовая задача Compose: самоподписанный сертификат для HTTPS-порта S3 SeaweedFS (8334).
# Нужен pgBackRest: он ходит в S3 только по TLS. В проде копии уходят в хранилище провайдера с
# обычным сертификатом. Повторный запуск ничего не пересоздаёт, но каждый раз приводит права к норме.
#
# Том s3tls (ключ и сертификат) монтируется только в SeaweedFS: ключ принадлежит его пользователю
# (uid 1000) и закрыт для остальных. Том s3ca получает копию одного сертификата: его читают
# postgres, pgbackrest и restore-drill как корневой (repo1-storage-ca-file).
set -eu

TLS_DIR="${TLS_DIR:-/tls}"
CA_DIR="${CA_DIR:-/ca}"
SEAWEED_UID="${SEAWEED_UID:-1000}"

if [ -s "$TLS_DIR/server.crt" ] && [ -s "$TLS_DIR/server.key" ]; then
  echo "gen-cert: сертификат уже есть"
else
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=seaweedfs" -addext "subjectAltName=DNS:seaweedfs,DNS:localhost" \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" 2>/dev/null
  echo "gen-cert: сертификат создан"
fi

chown "$SEAWEED_UID:$SEAWEED_UID" "$TLS_DIR/server.key" "$TLS_DIR/server.crt"
chmod 0400 "$TLS_DIR/server.key"
chmod 0444 "$TLS_DIR/server.crt"
cp "$TLS_DIR/server.crt" "$CA_DIR/server.crt"
chmod 0444 "$CA_DIR/server.crt"

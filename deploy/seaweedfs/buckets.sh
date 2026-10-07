#!/bin/sh
# Создаёт buckets SeaweedFS (S3_BUCKETS: список через пробел), когда master и filer ответят.
# Запускается обёрткой entrypoint.sh в фоне внутри контейнера SeaweedFS; повтор безопасен.
# `buckets.sh --check` (healthcheck): код 0, если все buckets уже есть.
set -eu

MASTER="${MASTER:-127.0.0.1:9333}"
FILER="${FILER:-127.0.0.1:8888}"
BUCKETS="${S3_BUCKETS:-media}"

all_present() {
  for bucket in $BUCKETS; do
    wget -q -O /dev/null "http://$FILER/buckets/$bucket/" 2>/dev/null || return 1
  done
}

if [ "${1:-}" = "--check" ]; then
  all_present
  exit $?
fi

tries=0
until all_present; do
  for bucket in $BUCKETS; do
    if ! wget -q -O /dev/null "http://$FILER/buckets/$bucket/" 2>/dev/null; then
      printf 's3.bucket.create -name %s\n' "$bucket" | weed shell -master="$MASTER" >/dev/null 2>&1 || true
    fi
  done
  tries=$((tries + 1))
  if [ "$tries" -ge 120 ]; then
    echo "buckets: SeaweedFS не создал buckets ($BUCKETS) за 4 минуты" >&2
    exit 1
  fi
  sleep 2
done
echo "buckets: готовы ($BUCKETS)"

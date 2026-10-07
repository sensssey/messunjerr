#!/usr/bin/env bash
# Дымовой тест prod-подобного стенда (S4-03): `deploy/smoke.sh [ТЕГ]`, код 0 при успехе.
# С ТЕГом проверяет ещё и то, что обе реплики API запущены из этой сборки. Выкладка
# (deploy/rollout.sh) запускает его перед началом (стенд должен быть здоров, иначе перезапуск реплики
# дал бы простой) и после перекатки всех реплик и воркеров; в S21 к нему добавится синтетический вход.
#
# Проверки: обе реплики принимают трафик (/health/serving) и готовы изнутри (/health/ready:
# PostgreSQL, Redis, миграции на head или впереди кода),
# через Caddy по HTTPS работает /api/v1/meta, служебные адреса снаружи закрыты, HTTP уходит на HTTPS,
# корневые адреса не отдают лишних заголовков.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно, второй набор контейнеров сел бы на те же
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
STATE_DIR="${STATE_DIR:-deploy/.stand}"
SITE="${SITE_ADDRESS:-messunjerr.localhost}"
BASE_URL="${BASE_URL:-https://$SITE}"
CA_FILE="${CA_FILE-$STATE_DIR/root.crt}"
EXPECT_TAG="${1:-}"

fail() { printf 'smoke: ПРОВАЛ: %s\n' "$*" >&2; exit 1; }
ok() { printf 'smoke: ok: %s\n' "$*"; }

# Корневой сертификат внутреннего центра Caddy: берём из контейнера, если ещё не выгружен.
CURL=(curl -sS --max-time 15 --resolve "$SITE:443:127.0.0.1" --resolve "$SITE:80:127.0.0.1")
if [ -n "$CA_FILE" ]; then
  if [ ! -s "$CA_FILE" ]; then
    mkdir -p "$(dirname "$CA_FILE")"
    "${COMPOSE[@]}" cp caddy:/data/caddy/pki/authorities/local/root.crt "$CA_FILE" >/dev/null 2>&1 \
      || fail "нет корневого сертификата Caddy ($CA_FILE): стенд поднят?"
  fi
  CURL+=(--cacert "$CA_FILE" --ssl-no-revoke)
fi

# Код ответа без `-o /dev/null`: curl.exe на Windows такого файла не знает.
status_of() {
  local out
  out="$("${CURL[@]}" -w '\n%{http_code}' "$@")" || return 1
  printf '%s' "${out##*$'\n'}"
}

# 1. Реплики API: готовность изнутри и версия сборки.
for service in api-a api-b; do
  "${COMPOSE[@]}" exec -T "$service" python -m messunjerr healthcheck \
    --url http://127.0.0.1:8000/health/serving >/dev/null 2>&1 || fail "$service не принимает трафик (/health/serving)"
  "${COMPOSE[@]}" exec -T "$service" python -m messunjerr healthcheck \
    --url http://127.0.0.1:8000/health/ready >/dev/null 2>&1 || fail "$service не готов (/health/ready)"
  build="$("${COMPOSE[@]}" exec -T "$service" python -c \
    "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/meta'))['build'])")"
  if [ -n "$EXPECT_TAG" ] && [ "$build" != "$EXPECT_TAG" ]; then
    fail "$service запущен из сборки $build, ожидалась $EXPECT_TAG"
  fi
  ok "$service готов, сборка $build"
done

# 2. Через Caddy: /api/v1/meta по HTTPS (сертификат проверяется корневым сертификатом Caddy).
code="$(status_of "$BASE_URL/api/v1/meta")" || fail "$BASE_URL/api/v1/meta недоступен"
[ "$code" = "200" ] || fail "$BASE_URL/api/v1/meta ответил $code"
ok "$BASE_URL/api/v1/meta: 200"

# 3. Служебные адреса снаружи закрыты.
for path in /health/live /health/serving /health/ready /metrics; do
  code="$(status_of "$BASE_URL$path")" || fail "$BASE_URL$path недоступен"
  [ "$code" = "404" ] || fail "$BASE_URL$path снаружи отвечает $code, ожидалось 404"
done
ok "/health/* и /metrics снаружи: 404"

# 4. HTTP уходит на HTTPS.
code="$(status_of "http://$SITE/api/v1/meta")" || fail "http://$SITE недоступен"
[ "$code" = "308" ] || [ "$code" = "301" ] || fail "http://$SITE ответил $code, ожидался редирект"
ok "HTTP перенаправляется на HTTPS ($code)"

# 5. Ни Server, ни внутренних заголовков хранилища наружу не торчит.
headers="$("${CURL[@]}" -I "$BASE_URL/api/v1/meta" | tr -d '\r')"
if printf '%s\n' "$headers" | grep -qiE '^(server|x-powered-by):'; then
  fail "в ответе есть заголовок Server или X-Powered-By"
fi
ok "заголовки: лишнего нет"

# 6. Веб-интерфейс Mailpit стенда (только loopback хоста; порт публикуется через сеть edge).
if [ "$SITE" = "messunjerr.localhost" ]; then
  mailpit_port="${STAND_MAILPIT_PORT:-8026}"
  code="$(status_of "http://127.0.0.1:$mailpit_port/livez")" \
    || fail "Mailpit не отвечает на http://127.0.0.1:$mailpit_port (письма воркера не прочитать)"
  [ "$code" = "200" ] || fail "Mailpit ответил $code на /livez"
  ok "Mailpit: http://127.0.0.1:$mailpit_port"
fi

echo "smoke: ВСЁ В ПОРЯДКЕ"

#!/usr/bin/env bash
# Память и ядра контейнеров стенда против лимитов VPS 8 ГБ / 4 vCPU (S4-01): `deploy/stand-stats.sh`.
# Показывает текущее потребление, заданные лимиты, были ли убийства по памяти (OOM) и перезапуски,
# и сумму лимитов: она должна оставлять место под ОС и кэш страниц файловой системы.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'  # Git Bash не должен «чинить» аргументы, похожие на пути
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT="messunjerr-stand"  # не настраивается: тома стенда названы явно, второй набор контейнеров сел бы на те же
VPS_MIB="${VPS_MEMORY_MIB:-8192}"

ids="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT")"
[ -n "$ids" ] || { echo "стенд не запущен: ./dev.ps1 up-prod-like" >&2; exit 1; }

# shellcheck disable=SC2086 # список идентификаторов должен разойтись на отдельные аргументы
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' $ids
echo

total=0
for id in $ids; do
  name="$(docker inspect --format '{{.Name}}' "$id")"
  limit="$(docker inspect --format '{{.HostConfig.Memory}}' "$id")"
  cpuset="$(docker inspect --format '{{.HostConfig.CpusetCpus}}' "$id")"
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$id")"
  restarts="$(docker inspect --format '{{.RestartCount}}' "$id")"
  if [ "$limit" -gt 0 ]; then
    total=$((total + limit / 1024 / 1024))
    shown="$((limit / 1024 / 1024)) МиБ"
  else
    shown="без лимита"
  fi
  printf '%-36s лимит %-11s ядра %-4s OOM=%-5s перезапусков=%s\n' "${name#/}" "$shown" "${cpuset:--}" "$oom" "$restarts"
done
echo
echo "сумма лимитов: $total МиБ из $VPS_MIB МиБ VPS (остаток под ОС и кэш страниц: $((VPS_MIB - total)) МиБ), все контейнеры делят ядра 0-3"

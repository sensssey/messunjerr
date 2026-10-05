<#
.SYNOPSIS
Команды разработки бэкенда v2 в Docker: то же, что Makefile, для Windows без make.

.EXAMPLE
./dev.ps1 init
./dev.ps1 up
./dev.ps1 test -k outbox -x
./dev.ps1 revision "add users"
#>
# Именованных параметров нет намеренно: PowerShell сопоставляет сокращения (`-c` с `-Cmd`) и украл бы
# флаги pytest и psql. Все токены приходят в $args как есть.
$Cmd = if ($args.Count -gt 0) { [string]$args[0] } else { 'help' }
$Rest = if ($args.Count -gt 1) { @($args[1..($args.Count - 1)] | ForEach-Object { [string]$_ }) } else { @() }

Set-Location -LiteralPath $PSScriptRoot

$ComposeArgs = @('compose', '-f', 'deploy/compose.dev.yml')
$Code = 'src tests_v2 migrations'

# docker пишет прогресс в stderr, поэтому об ошибке судим только по коду возврата.
function Invoke-Docker {
    docker @args
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
function Invoke-Compose { Invoke-Docker @ComposeArgs @args }
function Invoke-Tools { Invoke-Compose run --rm tools @args }
# Без базы и Redis: для линтеров и проверки типов.
function Invoke-ToolsIsolated { Invoke-Compose run --rm --no-deps tools @args }

function Get-EnvValue([string]$Name, [string]$Default) {
    $file = Join-Path $PSScriptRoot 'deploy/.env'
    if (Test-Path -LiteralPath $file) {
        $line = Select-String -LiteralPath $file -Pattern "^\s*$Name=(.*)$" | Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    }
    return $Default
}

function Invoke-Lint { Invoke-ToolsIsolated sh -c "ruff check $Code && ruff format --check $Code" }
function Invoke-Typecheck { Invoke-ToolsIsolated pyright }
function Invoke-Arch { Invoke-ToolsIsolated lint-imports }
function Invoke-Tests { Invoke-Tools pytest -c pyproject.toml tests_v2 @Rest }

function Show-Help {
    @'
Команды (./dev.ps1 <команда>):

  init        создать deploy/.env со случайными паролями (один раз)
  up          поднять PostgreSQL, Redis, Mailpit, API и воркеры; миграции применяются сами
  down        остановить стек (данные сохраняются)
  restart     перезапустить API
  reset       остановить стек и удалить его данные (тома PostgreSQL и Redis)
  ps          состояние сервисов
  logs        логи API (./dev.ps1 logs postgres: другого сервиса)
  test        все тесты (аргументы pytest: ./dev.ps1 test -k outbox -x)
  test-unit   только unit-тесты, без базы и Redis
  lint        ruff: проверка кода и форматирования
  format      ruff: исправить найденное и отформатировать
  typecheck   pyright в строгом режиме
  arch        import-linter: границы между модулями
  check       lint + typecheck + arch + test: всё, что проверяет CI
  migrate     роли, база и миграции до последней ревизии
  db-check    alembic check: модели и миграции не расходятся
  revision    новая миграция по моделям: ./dev.ps1 revision "описание"
  psql        psql в базе разработки (суперпользователь)
  redis-cli   redis-cli в Redis разработки
  shell       bash в контейнере с инструментами
  lock        обновить backend/uv.lock после правки зависимостей
'@ | Write-Host
}

switch ($Cmd) {
    'help' { Show-Help }
    'init' {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            py -3 scripts/init_env.py @Rest
        }
        else {
            python scripts/init_env.py @Rest
        }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    'up' {
        Invoke-Compose up -d --build --wait api worker-default mailpit
        Write-Host 'API:     http://localhost:8000/api/v1/docs'
        Write-Host 'Mailpit: http://localhost:8025'
    }
    'down' { Invoke-Compose down }
    'restart' { Invoke-Compose restart api }
    'reset' { Invoke-Compose down -v --remove-orphans }
    'ps' { Invoke-Compose ps -a }
    'logs' {
        $services = if ($Rest.Count -gt 0) { $Rest } else { @('api') }
        Invoke-Compose logs -f --tail=100 @services
    }
    'test' { Invoke-Tests }
    'test-unit' { Invoke-ToolsIsolated pytest -c pyproject.toml tests_v2/unit @Rest }
    'lint' { Invoke-Lint }
    'format' { Invoke-ToolsIsolated sh -c "ruff check --fix $Code && ruff format $Code" }
    'typecheck' { Invoke-Typecheck }
    'arch' { Invoke-Arch }
    'check' {
        Invoke-Lint
        Invoke-Typecheck
        Invoke-Arch
        Invoke-Tests
    }
    'migrate' { Invoke-Compose run --rm migrate }
    'db-check' { Invoke-Tools alembic check }
    'revision' {
        if ($Rest.Count -eq 0) {
            Write-Host 'Укажите описание: ./dev.ps1 revision "add users"'
            exit 1
        }
        Invoke-Tools alembic revision --autogenerate -m ($Rest -join ' ')
    }
    'psql' { Invoke-Compose exec postgres psql -U postgres -d (Get-EnvValue 'DB_NAME' 'messunjerr') }
    'redis-cli' { Invoke-Compose exec redis sh -c 'redis-cli -a $REDIS_PASSWORD --no-auth-warning' }
    'shell' { Invoke-Tools bash }
    'lock' {
        $backend = Join-Path $PSScriptRoot 'backend'
        Invoke-Compose run --rm --no-deps `
            -v "$backend\pyproject.toml:/app/pyproject.toml" `
            -v "$backend\uv.lock:/app/uv.lock" `
            tools uv lock
    }
    default {
        Write-Host "Неизвестная команда: $Cmd"
        Show-Help
        exit 1
    }
}

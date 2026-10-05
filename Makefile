# Команды разработки бэкенда v2. Всё выполняется в Docker: на машине нужны только Docker с Compose v2
# и make (на Windows без make есть `./dev.ps1 <команда>` с тем же набором команд).
#
#   make init    создать deploy/.env со случайными паролями (один раз)
#   make up      собрать и поднять PostgreSQL, Redis, Mailpit и API: http://localhost:8000/api/v1/docs
#   make test    unit- и интеграционные тесты на настоящих PostgreSQL и Redis
#   make check   всё, что проверяет CI: линтер, типы, границы модулей, тесты
#
# Аргументы pytest: make test ARGS="-k outbox -x". Сообщение миграции: make revision m="add users".

# Значения (DB_NAME и др.) берём из deploy/.env, если он уже создан.
-include deploy/.env

COMPOSE := docker compose -f deploy/compose.dev.yml
RUN := $(COMPOSE) run --rm
TOOLS := $(RUN) tools
# Без базы и Redis: для линтеров и проверки типов.
TOOLS_ISOLATED := $(RUN) --no-deps tools
CODE := src tests_v2 migrations
DB_NAME ?= messunjerr
PYTHON ?= python3
ARGS ?=
m ?=

.DEFAULT_GOAL := help
.PHONY: help init up down restart reset ps logs test test-unit lint format typecheck arch check \
	migrate db-check revision psql redis-cli shell lock

help: ## Показать команды
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-11s %s\n", $$1, $$2}'

init: ## Создать deploy/.env со случайными паролями
	$(PYTHON) scripts/init_env.py

up: ## Поднять стек: PostgreSQL, Redis, Mailpit, API, воркеры (миграции применяются сами)
	$(COMPOSE) up -d --build --wait api worker-default mailpit
	@echo "API:     http://localhost:8000/api/v1/docs"
	@echo "Mailpit: http://localhost:8025"

down: ## Остановить стек (данные сохраняются)
	$(COMPOSE) down

restart: ## Перезапустить API
	$(COMPOSE) restart api

reset: ## Остановить стек и удалить его данные (тома PostgreSQL и Redis)
	$(COMPOSE) down -v --remove-orphans

ps: ## Состояние сервисов
	$(COMPOSE) ps -a

logs: ## Логи API (make logs s=postgres: логи другого сервиса)
	$(COMPOSE) logs -f --tail=100 $(or $(s),api)

test: ## Все тесты
	$(TOOLS) pytest -c pyproject.toml tests_v2 $(ARGS)

test-unit: ## Только unit-тесты (без базы и Redis)
	$(TOOLS_ISOLATED) pytest -c pyproject.toml tests_v2/unit $(ARGS)

lint: ## ruff: проверка кода и форматирования
	$(TOOLS_ISOLATED) sh -c "ruff check $(CODE) && ruff format --check $(CODE)"

format: ## ruff: исправить найденное и отформатировать
	$(TOOLS_ISOLATED) sh -c "ruff check --fix $(CODE) && ruff format $(CODE)"

typecheck: ## pyright в строгом режиме
	$(TOOLS_ISOLATED) pyright

arch: ## import-linter: границы между модулями
	$(TOOLS_ISOLATED) lint-imports

check: lint typecheck arch test ## Всё, что проверяет CI

migrate: ## Роли, база и миграции до последней ревизии
	$(RUN) migrate

db-check: ## alembic check: модели и миграции не расходятся
	$(TOOLS) alembic check

revision: ## Новая миграция по моделям: make revision m="описание"
	@test -n "$(m)" || (echo 'Укажите описание: make revision m="add users"'; exit 1)
	$(TOOLS) alembic revision --autogenerate -m "$(m)"

psql: ## psql в базе разработки (суперпользователь)
	$(COMPOSE) exec postgres psql -U postgres -d $(DB_NAME)

redis-cli: ## redis-cli в Redis разработки
	$(COMPOSE) exec redis sh -c 'redis-cli -a "$$REDIS_PASSWORD" --no-auth-warning'

shell: ## bash в контейнере с инструментами
	$(TOOLS) bash

lock: ## Обновить backend/uv.lock после правки зависимостей в pyproject.toml
	$(RUN) --no-deps -v "$(CURDIR)/backend/pyproject.toml:/app/pyproject.toml" -v "$(CURDIR)/backend/uv.lock:/app/uv.lock" tools uv lock

# Команды разработки бэкенда v2. Всё выполняется в Docker: на машине нужны только Docker с Compose v2
# и make (на Windows без make есть `./dev.ps1 <команда>` с тем же набором команд).
#
#   make init    создать deploy/.env со случайными паролями (один раз)
#   make up      собрать и поднять PostgreSQL, Redis, Mailpit и API: http://localhost:8000/api/v1/docs
#   make test    unit- и интеграционные тесты на настоящих PostgreSQL и Redis
#   make check   всё, что проверяет CI: линтер, типы, границы модулей, тесты
#
# Прод-подобный стенд (S4: Caddy, лимиты VPS, SeaweedFS, pgBackRest), отдельный проект Compose:
#   make up-prod-like   поднять стенд: https://messunjerr.localhost/api/v1/meta
#   make stand-test     тесты стенда через Caddy;  make deploy TAG=v2 / make rollback: выкладка без простоя
#   make backup / restore-drill: копия pgBackRest и учение по восстановлению на отдельной БД
#   make rehearse-migration: репетиция выкладки и отката с настоящей новой ревизией Alembic
#
# Аргументы pytest: make test ARGS="-k outbox -x". Сообщение миграции: make revision m="add users".

# Значения (DB_NAME и др.) берём из deploy/.env, если он уже создан.
-include deploy/.env

COMPOSE := docker compose -f deploy/compose.dev.yml
RUN := $(COMPOSE) run --rm
TOOLS := $(RUN) tools
# Без базы и Redis: для линтеров и проверки типов.
TOOLS_ISOLATED := $(RUN) --no-deps tools
# Проект стенда задаём явно: имя из deploy/.env не должно подменить его на проект dev-стека.
STAND := docker compose -p messunjerr-stand -f deploy/compose.yml
CODE := src tests_v2 migrations
DB_NAME ?= messunjerr
PYTHON ?= python3
ARGS ?=
m ?=

.DEFAULT_GOAL := help
.PHONY: help init up down restart reset ps logs test test-unit lint format typecheck arch check \
	migrate db-check revision seed psql redis-cli shell lock \
	up-prod-like down-prod-like reset-prod-like ps-prod-like logs-prod-like stand-test stand-ca \
	stand-stats stand-reset-limits smoke deploy rollback rehearse-migration backup backup-status restore-drill

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

seed: ## Учебные аккаунты с профилями (make seed ARGS="--users 100"), повтор безопасен
	$(TOOLS) python -m messunjerr seed $(ARGS)

psql: ## psql в базе разработки (суперпользователь)
	$(COMPOSE) exec postgres psql -U postgres -d $(DB_NAME)

redis-cli: ## redis-cli в Redis разработки
	$(COMPOSE) exec redis sh -c 'redis-cli -a "$$REDIS_PASSWORD" --no-auth-warning'

shell: ## bash в контейнере с инструментами
	$(TOOLS) bash

lock: ## Обновить backend/uv.lock после правки зависимостей в pyproject.toml
	$(RUN) --no-deps -v "$(CURDIR)/backend/pyproject.toml:/app/pyproject.toml" -v "$(CURDIR)/backend/uv.lock:/app/uv.lock" tools uv lock

# ----------------------------------------------------------------------------- прод-подобный стенд
up-prod-like: ## Стенд как на VPS: Caddy, лимиты 8 ГБ/4 ядра, SeaweedFS, pgBackRest (https://messunjerr.localhost)
	bash deploy/up.sh

down-prod-like: ## Остановить стенд (данные сохраняются)
	$(STAND) down

reset-prod-like: ## Остановить стенд и удалить его тома (dev-стек не затрагивается)
	$(STAND) --profile '*' down -v --remove-orphans
	rm -f deploy/.stand/current_tag deploy/.stand/previous_tag deploy/.stand/pending

ps-prod-like: ## Состояние сервисов стенда
	$(STAND) ps -a

logs-prod-like: ## Логи стенда (make logs-prod-like s=caddy)
	$(STAND) logs -f --tail=100 $(or $(s),api-a)

stand-test: ## Тесты стенда через Caddy (ARGS="-k media")
	$(STAND) run --rm stand-tools pytest -c pyproject.toml tests_v2/stand $(ARGS)

stand-ca: ## Выгрузить корневой сертификат Caddy и показать, как ему доверять
	@mkdir -p deploy/.stand && $(STAND) cp caddy:/data/caddy/pki/authorities/local/root.crt deploy/.stand/root.crt
	@echo "Корневой сертификат: deploy/.stand/root.crt. Доверять ему нужно один раз (решение за вами):"
	@echo "  Linux (Debian/Ubuntu): sudo cp deploy/.stand/root.crt /usr/local/share/ca-certificates/messunjerr-caddy.crt && sudo update-ca-certificates"
	@echo "  macOS: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain deploy/.stand/root.crt"
	@echo "  Firefox: Настройки, Сертификаты, Центры сертификации, Импортировать"

stand-stats: ## Память и ядра контейнеров стенда против лимитов VPS
	bash deploy/stand-stats.sh

stand-reset-limits: ## Сбросить счётчики лимитов запросов в Redis стенда (после многократных прогонов тестов)
	bash deploy/reset-limits.sh

smoke: ## Дымовой тест стенда
	bash deploy/smoke.sh $(TAG)

deploy: ## Выкладка без простоя: make deploy TAG=v2 (без TAG берётся метка времени)
	bash deploy/rollout.sh deploy $(TAG) $(ARGS)

rollback: ## Откат на предыдущую выкладку
	bash deploy/rollout.sh rollback $(ARGS)

rehearse-migration: ## Репетиция: выкладка и откат с настоящей новой ревизией Alembic (БД возвращается)
	bash deploy/rehearse-migration.sh

backup: ## Копия pgBackRest по требованию: make backup TYPE=full|diff|incr
	$(STAND) exec -T pgbackrest /bin/sh /backup/now.sh $(or $(TYPE),diff)

backup-status: ## Список копий и возраст последней
	$(STAND) exec -T pgbackrest /bin/sh /backup/status.sh

restore-drill: ## Учение: восстановить копию на отдельной БД и проверить (боевая не затрагивается)
	bash deploy/restore-drill.sh

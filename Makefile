# Канонические команды проекта (P-12: один способ сделать вещь).
# Порядок работы: make venv → make check

VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv check fmt test migrate seed dev dev-down dev-logs smoke deploy-staging

venv: ## Создать окружение и установить зависимости
	python3 -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet -e ".[dev]"

check: ## Полная проверка: формат + линтер + границы импортов + типы + тесты (то же, что CI)
	$(BIN)/ruff format --check src tests alembic
	$(BIN)/ruff check src tests alembic
	$(BIN)/lint-imports
	$(BIN)/mypy
	$(BIN)/pytest

fmt: ## Автоформатирование кода
	$(BIN)/ruff format src tests alembic
	$(BIN)/ruff check --fix src tests alembic

test: ## Только тесты
	$(BIN)/pytest

migrate: ## Применить миграции БД к локальной среде (make dev должен быть поднят)
	$(BIN)/alembic upgrade head

seed: ## Создать демо-тенанта Demo Hotel и его категории заявок (идемпотентно; make dev и make migrate уже выполнены)
	$(BIN)/python -m hospitality.tools.seed

dev: ## Поднять локальную среду одной командой: Postgres+pgvector, Redis, приложение
	@test -f .env || cp .env.example .env
	docker compose -f ops/docker-compose.yml --env-file .env up -d --build

dev-down: ## Остановить локальную среду
	docker compose -f ops/docker-compose.yml --env-file .env down

dev-logs: ## Логи локальной среды
	docker compose -f ops/docker-compose.yml --env-file .env logs -f

smoke: ## Проверить, что среда поднимается и Postgres/Redis отвечают
	./ops/smoke.sh

deploy-staging: ## Выкатить main на staging вручную (Task 0006; обычно деплой идёт сам при merge)
	gh workflow run ci.yml --ref main

# Канонические команды проекта (P-12: один способ сделать вещь).
# Порядок работы: make venv → make check

VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv check fmt test dev dev-down dev-logs smoke

venv: ## Создать окружение и установить зависимости
	python3 -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet -e ".[dev]"

check: ## Полная проверка: формат + линтер + границы импортов + типы + тесты (то же, что CI)
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests
	$(BIN)/lint-imports
	$(BIN)/mypy
	$(BIN)/pytest

fmt: ## Автоформатирование кода
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

test: ## Только тесты
	$(BIN)/pytest

dev: ## Поднять локальную среду одной командой: Postgres+pgvector, Redis, приложение
	@test -f .env || cp .env.example .env
	docker compose -f ops/docker-compose.yml --env-file .env up -d --build

dev-down: ## Остановить локальную среду
	docker compose -f ops/docker-compose.yml --env-file .env down

dev-logs: ## Логи локальной среды
	docker compose -f ops/docker-compose.yml --env-file .env logs -f

smoke: ## Проверить, что среда поднимается и Postgres/Redis отвечают
	./ops/smoke.sh

# Канонические команды проекта (P-12: один способ сделать вещь).
# Порядок работы: make venv → make check

VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv check fmt test

venv: ## Создать окружение и установить зависимости
	python3 -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet -e ".[dev]"

check: ## Полная проверка: формат + линтер + типы + тесты (то же, что CI)
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests
	$(BIN)/mypy
	$(BIN)/pytest

fmt: ## Автоформатирование кода
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

test: ## Только тесты
	$(BIN)/pytest

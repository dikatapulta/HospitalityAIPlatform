# AI Hospitality Platform

[![CI](https://github.com/dikatapulta/HospitalityAIPlatform/actions/workflows/ci.yml/badge.svg)](https://github.com/dikatapulta/HospitalityAIPlatform/actions/workflows/ci.yml)

Мультитенантная SaaS-платформа, где AI является основным интерфейсом взаимодействия гостей отеля, персонала и внутренних сервисов. Это не чат-бот: чат — лишь один из каналов, ядро — оркестрация гостиничных процессов.

## Документация (читать в этом порядке)

1. [CLAUDE.md](CLAUDE.md) — точка входа для LLM-разработчиков: карта проекта и обязательные правила.
2. [FOUNDATION.md](FOUNDATION.md) — инженерная конституция (Architecture Freeze v1).
3. [PROJECT_EXECUTION_PLAN.md](PROJECT_EXECUTION_PLAN.md) — генеральный план строительства (фазы 0–6).
4. [PHASE0.md](PHASE0.md) — текущая фаза: Walking Skeleton, задачи 0001–0020.
5. [docs/GLOSSARY.md](docs/GLOSSARY.md) — единый словарь терминов.
6. [docs/adr/](docs/adr/) — архитектурные решения.

## Быстрый старт

```bash
make venv    # создать окружение и установить зависимости
make check   # формат + линтер + границы импортов + типы + тесты (то же, что CI)
```

Требуется Python 3.12+.

## Локальная среда (Docker)

```bash
make dev       # поднять Postgres+pgvector, Redis и контейнер приложения (docker compose)
make smoke     # проверить, что Postgres и Redis отвечают
make dev-down  # остановить среду
```

Требуется Docker + Docker Compose. Настройки — в `.env` (создаётся из `.env.example` автоматически при первом `make dev`); секреты в репозитории не хранятся (FOUNDATION §11).

После `make dev` приложение доступно на `http://localhost:8000`; `curl http://localhost:8000/health/ready` возвращает 200 и JSON-статусы Postgres/Redis (Task 0005).

## Структура

```
src/hospitality/          # корневой пакет (имя "platform" без корня конфликтует со stdlib)
├── platform/             # kernel: тенанты, RBAC, аудит, конфигурация
├── shared/               # kernel: логирование, ошибки, события, БД
├── modules/              # доменные модули (requests, guests, ...)
├── ai/                   # композиционный слой: gateway, orchestrator, tools
├── channels/             # композиционный слой: telegram, whatsapp, email
└── integrations/         # адаптеры портов (opera, kaspi, google_maps, ...)
```

Направления зависимостей и правила границ: FOUNDATION.md §5.1, правило R-5. Границы проверяет import-linter (контракты в `pyproject.toml`, секция `[tool.importlinter]`) — нарушение слоёв не проходит CI.

## Статус

Phase 0 — Walking Skeleton, выполняется по [PHASE0.md](PHASE0.md).

---
name: verify
description: Как поднять и прогнать приложение локально для runtime-верификации изменений (uvicorn + docker db/redis), без полного compose-стека.
---

# Верификация HospitalityAIPlatform вживую

Поверхности: HTTP API (`hospitality.app`), воркер (`hospitality.worker`),
алертер (`hospitality.tools.alerter`) — все процессы из одного пакета.

## Запуск

```bash
# Зависимости (Postgres 5432 / Redis 6379); db обычно уже бежит у тестов:
docker compose -f ops/docker-compose.yml --env-file .env.example up -d db redis

# Приложение (порт 8010, чтобы не конфликтовать с make dev).
# --no-proxy-headers — тот же флаг, что в CMD образа (ops/Dockerfile) и в команде
# staging (ops/deploy/docker-compose.staging.yml): у uvicorn прослойка proxy-headers
# включена ПО УМОЛЧАНИЮ и подменяет адрес клиента заголовком X-Forwarded-For.
# Локально она НЕ инертна — сосед по сокету при запросе с той же машины ровно
# 127.0.0.1, то есть доверенный по умолчанию, — и проверка адреса руками показала бы
# картину, обратную staging (issue #248; почему флаг — src/hospitality/shared/clientip.py):
.venv/bin/uvicorn hospitality.app:app --port 8010 --no-proxy-headers

# Воркер / алертер — те же venv-процессы:
.venv/bin/python -m hospitality.worker
.venv/bin/python -m hospitality.tools.alerter
```

Миграции локальной БД: `make migrate` (нужен только при свежем volume).

## Точки наблюдения

- `curl -s localhost:8010/health/ready` — 200/503 + JSON checks.
- `curl -s localhost:8010/metrics` — Prometheus-текст (RED, outbox, llm_*).
- Логи процессов — JSON-строки в stdout; correlation id в каждом ответе
  (`X-Correlation-ID`).
- Telegram-исходящие подменяются `TELEGRAM_API_BASE_URL=http://127.0.0.1:8099`
  на фейковый HTTP-сервер, пишущий sendMessage-тела в файл (у алертера —
  свои `TELEGRAM_ALERT_*`-переменные).

## Сценарий «уронить Postgres» (репетиция DoD Task 0018)

`docker stop ops-db-1` → ready 503, `outbox_pending_events NaN`, алерт
ERR-OPS-001 у алертера (порог/интервал ужимаются env:
`ALERT_POLL_INTERVAL_SECONDS=2 ALERT_READY_FAILURE_THRESHOLD=2`);
`docker start ops-db-1` → ✅-восстановление.

## Грабли

- Настройки читаются из env И `.env`; тестовые переопределения — только env
  (у pydantic-settings env важнее файла).
- `get_settings()` кэширован (`lru_cache`) — процесс перечитывает конфиг
  только при рестарте.
- Аутентифицированные эндпоинты `/api/v1/*` требуют `Authorization: Bearer
  dev-service-token` и засеянного тенанта (`make seed`).

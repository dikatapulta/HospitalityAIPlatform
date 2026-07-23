# ai/gateway — единственная дверь к LLM (Task 0014)

## Назначение

Все обращения к LLM — из любого модуля, для любой задачи — проходят через
этот пакет (FOUNDATION §7.2). Он отвечает за таймауты/ретраи, стоимость,
дневной бюджет тенанта и журнал каждого вызова (`llm_call_log`). Прямой
импорт SDK провайдера где-либо ещё запрещён и отлавливается import-linter'ом
(контракт 4 pyproject.toml, R-5). Маршрутизации моделей нет (Non-Goal
Task 0014): одна модель `LLM_MODEL`.

## Состав

| Файл | Что даёт |
| --- | --- |
| `api.py` | Публичный интерфейс: единственная точка импорта извне (R-5) |
| `schemas.py` | Pydantic-границы: `LlmMessage`, `LlmRequest`, `LlmResponse`, `ToolSpec`, `ToolCall` (R-6) |
| `provider.py` | Порт `LlmProvider` + `LlmProviderResult` + ошибки порта |
| `anthropic_provider.py` | Боевой адаптер Anthropic — единственное место `import anthropic` |
| `mock_provider.py` | `MockLlmProvider` — Fake-адаптер порта (ADR-007) для dev/CI/тестов |
| `models.py` | `LlmCallLog` — тенантный журнал вызовов (канон RLS) |
| `service.py` | `complete()`: бюджет → ретраи → стоимость → журнал + лог `llm_call` |
| `tests/` | Логирование/ретраи/бюджет на mock; контракт анфропик-адаптера на заглушке SDK |

## Публичный API (`api.py`)

- `complete(LlmRequest, provider=...) -> LlmResponse` — канонический вызов
  LLM; вызывается внутри `tenant_context(...)` (P-4). Без `provider` — боевой
  Anthropic из настроек; `provider` переопределяют тесты и композиция.
- `LlmMessage`, `LlmRequest`, `LlmResponse`, `ToolSpec`, `ToolCall` — схемы границ.
- `LlmProvider` — порт для новых адаптеров; `MockLlmProvider` /
  `ScriptedLlmProvider` (+`MockTurn`) — Fake-адаптеры (ADR-007) для тестов
  зависимых слоёв (оркестратор, Task 0015): один ответ и сценарий из ходов.
- `compute_prompt_hash(LlmRequest) -> str` — sha256-«версия промпта» (§7.2).
- Коды ошибок: `ERR_AI_PROVIDER_TIMEOUT` (ERR-AI-001, 503),
  `ERR_AI_BUDGET_EXCEEDED` (ERR-AI-002, 429),
  `ERR_AI_PROVIDER_ERROR` (ERR-AI-003, 502) — каталог `docs/runbooks/errors.md`.

## Инструменты (Task 0015/0017.1, §7.3)

`LlmRequest.tools` — список `ToolSpec` (`name`, `description`, `input_schema`
как JSON Schema). Провайдер передаёт их модели и возвращает запрошенные вызовы в
`LlmResponse.tool_calls` (`id`, `name`, `arguments`) вместе с `stop_reason`.
Набор инструментов входит в `prompt_hash` (часть «версии промпта», §7.2).
Gateway несёт только провайдер-facing поля инструмента; **класс подтверждения
(P-9) живёт в `ai/tools`, а не здесь** — им распоряжается оркестратор.

`LlmRequest.forced_tool` (Task 0017.1) — имя инструмента, который модель
ОБЯЗАНА вызвать: анфропик-адаптер транслирует в
`tool_choice={"type": "tool", "name": ...}`, свободный текстовый ответ
невозможен. `None` (по умолчанию) — прежнее поведение (auto). Используется для
структурных решений — классификация ответа гостя на гейте подтверждения P-9
(оркестратор, spec 0017.1). Поле входит в `prompt_hash`. Fake-провайдеры
сценарные и поле не интерпретируют — тесты видят его в `provider.calls`.

## Порядок вызова `complete()`

1. Дневной бюджет тенанта: сумма `cost_usd` за текущие UTC-сутки ≥
   `LLM_TENANT_DAILY_BUDGET_USD` → отказ ERR-AI-002 (с `Retry-After`),
   провайдер не вызывается. Бюджет одинаков для всех тенантов (Phase 0);
   пер-тенантный — конфиг тенанта, Phase 1.
2. До `LLM_MAX_ATTEMPTS` попыток; ретрай ТОЛЬКО по таймауту (SDK-ретраи
   у адаптера выключены — механизм один). Исчерпание — ERR-AI-001; другая
   ошибка провайдера — ERR-AI-003 без ретрая.
3. Стоимость — по `MODEL_PRICING_USD_PER_MTOK` (service.py, единственное
   место истины цен); модель вне прайс-листа — ошибка конфигурации (500).
4. Журнал: строка `llm_call_log` на КАЖДЫЙ исход (ok / timeout / error) +
   структурированное событие `llm_call` + метрики `llm_calls_total` /
   `llm_tokens_total` / `llm_cost_usd_total` по тенантам (`shared/metrics.py`,
   Task 0018, §10.7) — та же единая точка `_log_call`.

## События

Не публикует и не потребляет доменных событий.

## Таблицы (миграция `0007`, RLS — копия канона `0002`)

- `llm_call_log` — `id`, `tenant_id` (FK+индекс), `correlation_id`,
  `provider`, `model`, `prompt_hash` (sha256, сам текст промпта не хранится —
  PII, §7.6), `status` (`ok`/`timeout`/`error`), `input_tokens`,
  `output_tokens`, `cost_usd` (NUMERIC(12,6)), `latency_ms`,
  `created_at` (индекс — бюджетный запрос за сутки). Под RLS
  (ENABLE + FORCE + политика `tenant_isolation`).

## Конфигурация (shared/config.py, .env.example)

`ANTHROPIC_API_KEY` (пустой валиден для dev/CI — боевой адаптер при нём не
создастся), `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_ATTEMPTS`,
`LLM_TENANT_DAILY_BUDGET_USD`.

## Зависимости

Внутренние: `hospitality.shared` (db, tenancy, config, errors, logging).
Внешние сверх общих: `anthropic` (только внутри этого пакета — контракт 4).

## Типовые сценарии изменения

- **Новый LLM-провайдер** — адаптер порта `LlmProvider` в этом пакете +
  строки прайс-листа + SDK в `forbidden_modules` контракта 4 + контрактный
  тест адаптера. Наружу ничего не меняется.
- **Смена/добавление модели** — `LLM_MODEL` + строка в
  `MODEL_PRICING_USD_PER_MTOK`. Кандидаты гостевого диалога (Task 0015) —
  `claude-haiku-4-5` и `claude-sonnet-5` (оба уже в прайс-листе); финальный
  дефолт фиксируется bake-off'ом на 6 языках (spec 0015, §7.7). Маршрутизация
  «дешёвая/дорогая» — отдельная задача с ADR, не раньше Phase 1.
- **Пер-тенантный бюджет** — поле в `TenantConfig` (platform/config.py) и
  чтение его в `_ensure_tenant_budget` вместо общей настройки.
- **Приоритеты вызовов (диалог гостя важнее аналитики)** — §7.2, Phase 1+.

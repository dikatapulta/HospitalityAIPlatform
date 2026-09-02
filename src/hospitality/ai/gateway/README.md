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
| `models.py` | `LlmCallLog` — тенантный журнал вызовов (канон RLS); `LlmBudgetReservation` — резерв бюджета на время вызова |
| `service.py` | `complete()`: резерв → бюджет → ретраи с паузой → стоимость → журнал + лог `llm_call` → снятие резерва; `refresh_budget_metrics()` — снимок расхода к лимиту для `/metrics`; `validate_configured_model()` — fail-fast старта по прайс-листу |
| `spend.py` | `spend_usd_between()` — сумма `cost_usd` тенанта за окно, которое задаёт вызывающий (число сводки дня, spec 0035 §6); отдельным файлом, потому что `service.py` и без него за границей R-3 |
| `tests/` | Логирование/ретраи/бюджет на mock; резерв бюджета и пауза между попытками — `tests/test_budget_reservation.py`; контракт анфропик-адаптера на заглушке SDK (отказ старта на модели вне прайс-листа — `tests/test_llm_model_startup.py`: проверяются оба composition root'а) |

## Публичный API (`api.py`)

- `complete(LlmRequest, provider=...) -> LlmResponse` — канонический вызов
  LLM; вызывается внутри `tenant_context(...)` (P-4). Без `provider` — боевой
  Anthropic из настроек; `provider` переопределяют тесты и композиция.
- `LlmMessage`, `LlmRequest`, `LlmResponse`, `ToolSpec`, `ToolCall` — схемы границ.
- `LlmProvider` — порт для новых адаптеров; `MockLlmProvider` /
  `ScriptedLlmProvider` (+`MockTurn`) — Fake-адаптеры (ADR-007) для тестов
  зависимых слоёв (оркестратор, Task 0015): один ответ и сценарий из ходов.
- `compute_prompt_hash(LlmRequest) -> str` — sha256-«версия промпта» (§7.2).
- `refresh_budget_metrics()` — публикует в `/metrics` расход каждого тенанта за
  текущие UTC-сутки и его лимит (`llm_daily_spend_usd` / `llm_daily_budget_usd`,
  issue #103). Зовёт её composition root на каждый scrape: считать обязан
  владелец данных (`llm_call_log` под RLS), а kernel импортировать `ai/` не
  вправе (R-5). На ~80% лимита алертер шлёт ERR-OPS-006 — исчерпание бюджета
  перестало быть сюрпризом.
- `spend_usd_between(created_after=..., created_before=...) -> Decimal` — расход
  текущего тенанта на модель за произвольное окно (issue #301, spec 0035 §6):
  число строки «ИИ за сутки отеля: $1.84» в копии утренней сводки основателю.
  Границы задаёт вызывающий (окно полуоткрытое) — gateway про часовые пояса
  показа не знает; сводка передаёт сутки ОТЕЛЯ, и с дневным бюджетом ADR-017
  (всегда UTC-сутки) это число намеренно не совпадает: разные окна, одни данные.
- `validate_configured_model()` — fail-fast старта (issue #137): `LLM_MODEL` вне
  `MODEL_PRICING_USD_PER_MTOK` → процесс не поднимается с внятной ошибкой
  конфигурации (`SystemExit`, канон `shared/alerting.py`). Зовут её не напрямую,
  а через `hospitality/preflight.py` — общий список проверок старта: его гоняют
  оба composition root'а (`app.py`, `worker.py`) и, раньше их, ENTRYPOINT образа,
  потому что под супервизорами uvicorn (`--workers`, `--reload`) падение внутри
  рабочего процесса убивает только ребёнка (issue #267). Без
  `validate_configured_model()` неизвестный id (чаще всего
  датированный, `claude-sonnet-5-20250929`) вскрывался только в `_compute_cost`,
  то есть ПОСЛЕ оплаченного ответа провайдера: 500 у первого же гостя, и строки
  в `llm_call_log` нет — дневной бюджет слеп ровно на ошибочных вызовах.
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

1. Резерв бюджета (issue #46, ADR-017): строка `llm_budget_reservation` с
   пессимистичной оценкой стоимости этого вызова и сроком аренды. Ставится ДО
   проверки — иначе собственный вызов невидим параллельным, и проверка снова
   считает только прошлое.
2. Дневной бюджет тенанта: расход за текущие UTC-сутки (сумма `cost_usd`)
   ПЛЮС живые резервы > `LLM_TENANT_DAILY_BUDGET_USD` → отказ ERR-AI-002
   (с `Retry-After`), провайдер не вызывается. Бюджет одинаков для всех
   тенантов (Phase 0), умолчание — у самой настройки (`.env.example`,
   issue #125); пер-тенантный — конфиг тенанта, Phase 1 (spec 0014), бюджет
   как поле тарифа — биллинг Phase 5. Метрика `llm_daily_spend_usd` считает
   только записанный расход, без резервов (см. `refresh_budget_metrics`).
3. До `LLM_MAX_ATTEMPTS` попыток; ретрай ТОЛЬКО по таймауту (SDK-ретраи
   у адаптера выключены — механизм один). Между попытками — пауза
   `LLM_RETRY_BACKOFF_BASE_SECONDS × 2^(попытка−1)` с разбросом [0.5×, 1×] и
   потолком в один `LLM_TIMEOUT_SECONDS` (формула — канон ADR-009); после
   последней попытки паузы нет. Исчерпание — ERR-AI-001; другая ошибка
   провайдера — ERR-AI-003 без ретрая.
4. Стоимость — по `MODEL_PRICING_USD_PER_MTOK` (service.py, единственное
   место истины цен). Модели вне прайс-листа здесь уже быть не может: её
   отсекает `validate_configured_model()` на старте процесса. Ветка `ValueError`
   в `_compute_cost` осталась страховкой для провайдера, собранного мимо
   настроек (`build_anthropic_provider` с произвольной моделью — bake-off).
5. Журнал: строка `llm_call_log` на КАЖДЫЙ исход (ok / timeout / error) +
   структурированное событие `llm_call` + метрики `llm_calls_total` /
   `llm_tokens_total` / `llm_cost_usd_total` по тенантам (`shared/metrics.py`,
   Task 0018, §10.7) — та же единая точка `_log_call`.
6. Снятие резерва — в `finally`, ПОСЛЕ записи исхода: между «резерва нет» и
   «расход виден» не остаётся окна, в котором вызов невидим обеим проверкам.

## События

Не публикует и не потребляет доменных событий.

## Таблицы (миграции `0007` и `0023`, RLS — копия канона `0002`)

- `llm_call_log` — `id`, `tenant_id` (FK+индекс), `correlation_id`,
  `provider`, `model`, `prompt_hash` (sha256, сам текст промпта не хранится —
  PII, §7.6), `status` (`ok`/`timeout`/`error`), `input_tokens`,
  `output_tokens`, `cost_usd` (NUMERIC(12,6)), `latency_ms`,
  `created_at` (индекс — бюджетный запрос за сутки). Под RLS
  (ENABLE + FORCE + политика `tenant_isolation`).
- `llm_budget_reservation` — `id`, `tenant_id` (FK+индекс), `amount_usd`
  (NUMERIC(12,6) — пессимистичная оценка одного вызова), `reserved_until`
  (индекс — срок аренды, ADR-016), `created_at`. Под тем же RLS. Строка живёт
  от проверки бюджета до записи исхода; истёкшие в сумму не входят и удаляются
  следующим резервом того же тенанта — фоновой задачи для них нет (NG-8).

## Конфигурация (shared/config.py, .env.example)

`ANTHROPIC_API_KEY` (пустой валиден для dev/CI — боевой адаптер при нём не
создастся), `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_ATTEMPTS`,
`LLM_RETRY_BACKOFF_BASE_SECONDS`, `LLM_TENANT_DAILY_BUDGET_USD`.

## Зависимости

Внутренние: `hospitality.shared` (db, tenancy, config, errors, logging).
Внешние сверх общих: `anthropic` (только внутри этого пакета — контракт 4).

## Типовые сценарии изменения

- **Новый LLM-провайдер** — адаптер порта `LlmProvider` в этом пакете +
  строки прайс-листа + SDK в `forbidden_modules` контракта 4 + контрактный
  тест адаптера. Наружу ничего не меняется.
- **Смена/добавление модели** — `LLM_MODEL` + строка в
  `MODEL_PRICING_USD_PER_MTOK` (порядок неважен, но без второго процесс не
  стартует — см. `validate_configured_model`). Кандидаты гостевого диалога (Task 0015) —
  `claude-haiku-4-5` и `claude-sonnet-5` (оба уже в прайс-листе); финальный
  дефолт фиксируется bake-off'ом на 6 языках (spec 0015, §7.7). Маршрутизация
  «дешёвая/дорогая» — отдельная задача с ADR, не раньше Phase 1.
- **Пер-тенантный бюджет** — поле в `TenantConfig` (platform/config.py) и
  чтение его в `_ensure_tenant_budget` вместо общей настройки; тем же местом
  правится и снимок метрик (`refresh_budget_metrics`), иначе алерт ERR-OPS-006
  начнёт сравнивать расход с чужим лимитом.
- **Приоритеты вызовов (диалог гостя важнее аналитики)** — §7.2, Phase 1+.

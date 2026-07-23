# Спека 0018 — Наблюдаемость: Sentry, метрики, минимальный трейсинг

- **Задача:** Task 0018 (PHASE0.md); FOUNDATION §10.4, §10.7, §10.8, §10.12;
  R-2/R-7/R-8. Класс ревью — A (kernel `shared/`, `ai/gateway`, новый эндпоинт,
  staging-конфигурация).
- **Открывает:** 0019.

## Проблема

Система не сообщает о своих проблемах сама. Логи есть (§10.1–10.2), но их
никто не читает непрерывно: упавший на staging Postgres, всплеск 500-х или
сгоревший дневной бюджет LLM обнаружатся только при ручной проверке. До
первого пользователя (Phase 1) нужны три вещи: автоматический сбор
необработанных ошибок с контекстом, метрики в стандартном формате и активный
алерт в Telegram-канал команды.

## Решение — четыре части

### 1. Sentry (managed, §10.4 + §10.12)

Зависимость `sentry-sdk`; новый модуль `shared/sentry.py` с единственной
функцией `init_sentry(settings)`, вызываемой из обоих composition root'ов —
`app.py` (`create_app`) и `worker.py` (`main`). Пустой `SENTRY_DSN` — Sentry
выключен (канон «пустой секрет валиден», как `ANTHROPIC_API_KEY`): dev/CI
работают без внешнего сервиса, лог `sentry_disabled` фиксирует это явно.

- **Контекст события (§10.4):** глобальный `before_send`-хук читает
  `structlog.contextvars.get_contextvars()` и проставляет тэги `tenant_id` и
  `correlation_id`. Один механизм покрывает оба процесса: в HTTP-запросе
  contextvars биндят `CorrelationIdMiddleware`/`TenantContextMiddleware`, в
  воркере — `tenant_context()` на каждое событие. Ничего не дублируется в
  каждом обработчике.
- **Что уходит в Sentry:** только необработанные исключения (интеграции
  Starlette/FastAPI и штатный `sys.excepthook`). Ожидаемые `AppError`
  перехватываются `register_error_handlers` и в Sentry не попадают — они
  диагностируются каталогом ошибок (§10.5), а не трекером.
- **PII:** `send_default_pii=False` (умолчание SDK); тела запросов не
  отправляются. `traces_sample_rate` не задаётся (0) — производительность
  трассирует OTel в Phase 1, не Sentry.
- **Окружение:** `SENTRY_ENVIRONMENT` (умолчание `dev`, staging ставит
  `staging`) — события staging и будущего prod разделяются в одном проекте.
- Для тестов `init_sentry` принимает необязательный `transport` — события
  перехватываются в память, реальный DSN не нужен.

### 2. `/metrics` в Prometheus-формате (§10.7)

Зависимость `prometheus-client`; новый модуль `shared/metrics.py`:
объявления метрик, функции записи и роутер `GET /metrics`
(`text/plain; version=0.0.4`). Prometheus-сервера в Phase 0 нет и не
появляется (без Grafana-стека): формат стандартный, потребитель сегодня —
алертер (часть 3) и `curl` основателя, завтра — любой managed-scraper.

| Метрика | Тип | Лейблы | Откуда пишется |
| --- | --- | --- | --- |
| `http_requests_total` | Counter | `method`, `route`, `status` | `CorrelationIdMiddleware` — тот же `finally`, что пишет `http_request` |
| `http_request_duration_seconds` | Histogram | `method`, `route` | там же |
| `llm_calls_total` | Counter | `tenant_id`, `model`, `status` | `ai/gateway/service._log_call` — единая точка всех исходов (ok/timeout/error) |
| `llm_tokens_total` | Counter | `tenant_id`, `model`, `direction` | там же (только ok) |
| `llm_cost_usd_total` | Counter | `tenant_id`, `model` | там же (только ok) |
| `outbox_pending_events` | Gauge | — | запрос `COUNT(*) WHERE processed_at IS NULL` в момент scrape |

Решения:

- **`route` — шаблон маршрута** (`/api/v1/requests/{request_id}`), не сырой
  путь: сырой путь взрывает кардинальность (UUID в URL, сканеры). Немэтчнутые
  запросы (404 от сканеров) собираются под константой `unmatched` — след
  сканирования виден, кардинальность ограничена.
- **RED-статус** — класс `2xx`/`4xx`/`5xx`, не точный код: алертеру и человеку
  нужен именно класс, точный код есть в логах `http_request`.
- **`tenant_id` как лейбл LLM-метрик** — требование §10.7 («по тенантам»);
  кардинальность = число отелей, это десятки. RED-метрики по тенантам не
  режутся (роутов × тенантов не нужно никому в Phase 0).
- **Глубина outbox считается в момент scrape** через `platform_session_scope()`
  (outbox — кросс-тенантная инфраструктурная таблица, как в
  `deliver_pending_events`). Недоступная БД не роняет `/metrics` (алертер
  обязан продолжать читать счётчики 5xx): gauge выставляется в `NaN`, ошибка
  логируется. Метрики процесса-воркера в Phase 0 не экспонируются — его
  здоровье видно через глубину outbox (растёт = воркер стоит) и Sentry.
- **`/metrics` анонимен** — явное решение (§11: «анонимный доступ — явное
  решение, а не умолчание»), симметрично `/health/*`: PII в метриках нет,
  секретов нет; токен для алертера — лишняя связность Phase 0. Пересмотр — при
  выходе в прод (reverse-proxy/allowlist), фиксируется в «Известных
  отступлениях» README `shared`.

### 3. Алертер → Telegram-канал команды (§10.8)

Новый entrypoint `hospitality/tools/alerter.py` — простейший watchdog-цикл,
запускается как четвёртый сервис staging-стека **тем же образом приложения**
(канон «один образ, другая команда», §5.3, как `worker`). Отступление от буквы
карточки («`ops/` алертинг»): скрипт в `ops/` потребовал бы отдельного
механизма доставки файла на сервер; в образе он приезжает существующим
деплоем. В `ops/deploy/docker-compose.staging.yml` добавляется сервис
`alerter`; сам код — в `tools/` (композиционный слой, там же живут `seed` и
`publish_demo_event`).

Цикл раз в `ALERT_POLL_INTERVAL_SECONDS` (60):

1. `GET {ALERT_TARGET_BASE_URL}/health/ready` (таймаут 5 c). Не-200 или
   сетевая ошибка — счётчик подряд неудач; на
   `ALERT_READY_FAILURE_THRESHOLD`-й (2) подряд — алерт **ERR-OPS-001**
   (однократно, до восстановления). Первый же успех после алерта — сообщение
   о восстановлении, счётчик сбрасывается. DoD-арифметика: падение Postgres →
   алерт через ~2–3 минуты.
2. `GET {ALERT_TARGET_BASE_URL}/metrics`, суммируются `http_requests_total`
   со `status="5xx"`. Прирост с прошлого опроса ≥
   `ALERT_ERROR_SPIKE_THRESHOLD` (5) — алерт **ERR-OPS-002**, не чаще
   `ALERT_COOLDOWN_SECONDS` (900). Недоступный `/metrics` — пропуск шага
   (падение приложения целиком уже покрыто ERR-OPS-001).

Сообщение обязано содержать (§10.8): код ошибки, тенанта (для платформенных
алертов — `platform`), correlation id (для опросов — `—`), детали
(`checks` из тела ready / величину всплеска) и ссылку на
`docs/runbooks/alerts.md`. Отправка — прямой `sendMessage` Telegram Bot API
через `httpx`: канал гостевого бота (`channels/telegram`) сознательно **не**
переиспользуется — тракт алертов обязан работать, даже когда код каналов
сломан (алерт о поломке не может зависеть от поломанного).

Состояние — в памяти процесса (перезапуск = чистый лист, худший случай —
один повторный алерт); никакой БД, Redis и Grafana-стека. Логика цикла —
чистые функции (парсер метрик + машина состояний `AlertMonitor.evaluate()`),
покрываются юнит-тестами без сети.

Конфигурация: `TELEGRAM_ALERT_BOT_TOKEN` (можно тот же токен, что у
гостевого бота), `TELEGRAM_ALERT_CHAT_ID` (канал команды). Оба пустые —
алертер логирует `alerting_disabled` (WARNING, повтор раз в час) и пассивно
спит: деплой без настроенных алертов остаётся зелёным, а неконфигурация
видна в логах. Частично заполненная пара — ошибка конфигурации, немедленное
падение с внятным сообщением (fail-fast, как `Literal` у `LOG_LEVEL`).

Известное ограничение (в runbook): алертер живёт на том же сервере — смерть
всего VPS не заалертит. Лечится бесплатным внешним uptime-сервисом на
`/health/ready` (managed, §10.12) — ручной шаг основателя после этой задачи,
в DoD не входит.

### 4. OTel — только заготовка (R-11: трактовка зафиксирована)

Карточка: «OTel — только заготовка, полный трейсинг не раньше Phase 1, чтобы
не перепроектировать». Трактовка: **ни зависимостей, ни no-op-кода** —
мёртвый код хуже отсутствующего. Заготовка = зафиксированные точки
подключения:

- поле `trace_id` уже обязательно в каждой лог-записи (§10.1, `logging.py`) и
  ждёт значения;
- `init_sentry` и `configure_logging` вызываются из одних и тех же двух
  composition root'ов — `init_tracing()` Phase 1 встанет рядом одной строкой;
- план Phase 1 описан в `docs/runbooks/alerts.md` (раздел «Что дальше»):
  `opentelemetry-instrumentation-{fastapi,sqlalchemy,redis,httpx}` + процессор
  structlog, пишущий `trace_id` из активного span.

## Конфигурация

Новые поля `Settings` (+ `.env.example`, `.env.staging.example`,
`docker-compose.staging.yml`):

| Поле | Умолчание | Кто использует |
| --- | --- | --- |
| `sentry_dsn` | `""` (выключен) | app, worker |
| `sentry_environment` | `dev` | app, worker (staging → `staging`) |
| `telegram_alert_bot_token` | `""` | alerter |
| `telegram_alert_chat_id` | `""` | alerter |
| `alert_target_base_url` | `http://localhost:8000` | alerter (compose → `http://app:8000`) |
| `alert_poll_interval_seconds` | `60.0` | alerter |
| `alert_ready_failure_threshold` | `2` | alerter |
| `alert_error_spike_threshold` | `5` | alerter |
| `alert_cooldown_seconds` | `900.0` | alerter |
| `alert_runbook_url` | GitHub-ссылка на `docs/runbooks/alerts.md` | alerter |

Staging-compose: сервису `app` и `worker` добавляются `SENTRY_DSN` /
`SENTRY_ENVIRONMENT`; новый сервис `alerter` (тот же `${APP_IMAGE}`, команда
`python -m hospitality.tools.alerter`, `restart: unless-stopped`, без
healthcheck — как `worker`, без `depends_on` от `db`: алертер обязан жить,
когда БД мертва).

## Коды ошибок (§10.5, R-8)

- **ERR-OPS-001** — `/health/ready` недоступен или нездоров N опросов подряд.
- **ERR-OPS-002** — всплеск 5xx: прирост ≥ порога за интервал опроса.

Статьи — в `docs/runbooks/errors.md`; новый runbook `docs/runbooks/alerts.md`:
что значит каждый алерт, что проверить по шагам (docker compose ps / logs,
`/health/ready` изнутри сервера, Sentry), как проверить тракт алертов вручную,
ограничение «алертер на том же VPS», план OTel Phase 1.

## Контракты

- Публичный API `shared` расширяется: `metrics.record_http_request()`,
  `metrics.record_llm_call()`, `metrics.router`, `sentry.init_sentry()`.
  Существующие сигнатуры (`CorrelationIdMiddleware`, `complete()`, health)
  не меняются.
- `ai/gateway/service.py` — добавляется вызов `record_llm_call(...)` рядом со
  строкой журнала `llm_call`; поведение и схемы gateway не меняются.
- Слои (R-5): `shared/metrics.py` и `shared/sentry.py` — kernel; `ai` →
  `shared` разрешено; `tools/alerter.py` импортирует только `shared.config` и
  `shared.logging` + httpx. Новые контракты import-linter не нужны.
- Миграций БД нет.

## План тестов (R-7)

1. **Метрики отдаются (DoD):** TestClient — запрос к существующему эндпоинту →
   `GET /metrics` — 200, `text/plain`, содержит `http_requests_total` с
   лейблом `route` = шаблон (не сырой путь с UUID) и классом статуса.
2. Немэтчнутый путь (`/no/such/route`) → лейбл `route="unmatched"`.
3. **LLM-метрики:** `complete()` на `MockLlmProvider` внутри
   `tenant_context` → `llm_calls_total{status="ok"}` и `llm_cost_usd_total`
   выросли; ошибка провайдера → вырос `llm_calls_total{status="error"}`.
4. **Глубина outbox:** опубликованное недоставленное событие →
   `outbox_pending_events` ≥ 1 в выдаче `/metrics`.
5. **Sentry (DoD):** `init_sentry` с in-memory transport; необработанная
   ошибка в эндпоинте внутри контекста тенанта → событие захвачено, тэги
   `tenant_id`/`correlation_id` совпадают с ответом; `AppError` 4xx события
   НЕ порождает.
6. **Алертер (машина состояний, без сети):** порог подряд-неудач → один
   алерт ERR-OPS-001 + одно восстановление; всплеск 5xx → ERR-OPS-002 с
   cooldown; недоступный `/metrics` не алертит; парсер метрик на образце
   реальной выдачи prometheus_client.
7. Существующие тесты `shared`/gateway — зелёные без правок утверждений.

## Затронутые файлы

`pyproject.toml` (deps: `sentry-sdk`, `prometheus-client`);
`shared/sentry.py`, `shared/metrics.py` (новые), `shared/middleware.py`,
`shared/README.md`; `app.py`, `worker.py`; `ai/gateway/service.py`,
`ai/gateway/README.md`; `tools/alerter.py` (новый); `shared/config.py`,
`.env.example`, `ops/deploy/.env.staging.example`,
`ops/deploy/docker-compose.staging.yml`; `docs/runbooks/alerts.md` (новый),
`docs/runbooks/errors.md`; тесты `tests/` и `src/hospitality/ai/tests/`.

## DoD (карточка)

`make check` зелёный; метрики отдаются; нарочная ошибка попадает в Sentry с
тенантом и correlation id; **уроненный на staging Postgres порождает
сообщение в Telegram-канале команды в течение минут** (ручная проверка
основателя: `docker compose stop db` → алерт → `start db` → восстановление).

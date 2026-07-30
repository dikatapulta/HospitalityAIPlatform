# shared — общая инфраструктура kernel

Назначение: инфраструктурный фундамент, который используют все слои выше
(FOUNDATION §5.1). `shared` не знает ни о доменных модулях, ни о `platform/`,
ни об интеграциях — направление импортов проверяет import-linter (R-5).

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `config.py` | `get_settings()` — единственный способ читать конфигурацию окружения | 0005 |
| `health.py` | `/health/live`, `/health/ready` | 0005 |
| `logging.py` | `configure_logging()`, `get_logger(module=__name__)` — канон JSON-логов (§10.1) | 0007 |
| `middleware.py` | `CorrelationIdMiddleware`, `get_correlation_id(request)` (§10.2) | 0007 |
| `errors.py` | `AppError(code=...)`, конверт `ErrorResponse`, `register_error_handlers` (§10.5, R-8) | 0007 |
| `db.py` | `session_scope()` — канон сессии БД; `Base`, `UTCDateTime`, `utc_now()` (§6, §9) | 0008 |
| `tenancy.py` | `tenant_context()` — канон контекста тенанта (CANONICAL, P-4, ADR-003); `TenantContextMiddleware` + `chain_resolvers()` — цепочка звеньев `TenantResolver` (ADR-008 §6: сервисный токен → staff-сессия; звенья живут в `platform/auth.py`) | 0009, #48 |
| `events.py` | `DomainEvent`, `publish()`, `subscribe()`, `deliver_pending_events()`, `cleanup_processed_events()` — канон доменных событий: outbox, доставка с backoff, retention (P-6, P-8, ADR-005, ADR-009) | 0010, issue #18 |
| `sentry.py` | `init_sentry()` — сбор необработанных ошибок с тэгами tenant_id/correlation_id (§10.4, §10.12) | 0018 |
| `metrics.py` | Метрики Prometheus-формата + роутер `GET /metrics`: RED по эндпоинтам, LLM, rate-limit гостя, логины кабинета (spec 0033), глубина outbox (§10.7) | 0018 |
| `ratelimit.py` | `consume_rate_limit()` — канонический Redis-счётчик rate-limit (CANONICAL, fail-open); `create_redis_client()` — канон подключения к Redis (§6) | issue #41 |
| `pii.py` | `mask_payment_card_numbers()` — канон маскирования PAN в тексте (CANONICAL, NG-3): 13–19 цифр + Луна → `[card ****1234]`; применяется контрактом нормализации каналов (`channels/base.py`), переиспользовать в маскировании логов (#13) | issue #128, spec 0031 |

## Канонические паттерны (P-12: копируй, не изобретай)

**Логировать:**

```python
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)
logger.info("guest_checked_in", guest_id=guest_id)   # event — snake_case, контекст — kwargs
```

Каждая запись — JSON с обязательными полями `timestamp`, `level`, `tenant_id`,
`correlation_id`, `trace_id`, `module`, `event`. Correlation id привязывается
middleware автоматически; `tenant_id` заполнит Task 0009, `trace_id` — OpenTelemetry.
`print()` и `logging.getLogger()` в коде платформы не используются.

`configure_logging()` глушит логгер `httpx` до WARNING: на INFO он пишет полный
URL исходящего запроса, а у Telegram Bot API токен бота — часть пути, то есть
секрет утёк бы в логи (§11). Канонический след исходящих вызовов — структурные
события адаптеров, не сырой URL httpx (issue #63).

Тот же запрет для ВХОДЯЩИХ путей — `redact_secrets_in_path(path)`: маскирует
секретные сегменты (`/w/{slug}/b/***` — токен одноразовой bind-ссылки,
spec 0033 §6), оставляя маршрут читаемым. Зовут во всех точках, где путь
покидает процесс: access-log `http_request` (`middleware.py`) и `before_send`
Sentry — там путь приходит тремя полями сразу (`request.url`, `query_string`
и заголовок `referer` с полным адресом страницы). `send_default_pii=False`
режет куки, тело и IP, но ни путь, ни `referer` в его список не входят;
локальные переменные фреймов Sentry не отправляет вовсе
(`include_local_variables=False` — в них лежит сырой `scope` с путём).
Появился новый секрет в пути — строка в `_SECRET_PATH_PATTERNS` и тест рядом
(ревью PR #155).

**Ожидаемая ошибка:**

```python
from hospitality.shared.errors import AppError

raise AppError(
    code="ERR-REQUESTS-001",          # код обязан иметь статью в docs/runbooks/errors.md
    message="Категория заявки не настроена у тенанта",  # виден клиенту — без внутренностей
    status_code=409,
)
```

Ответ API при любой ошибке — один конверт (P-7):
`{"error": {"code": ..., "message": ..., "correlation_id": ...}}`.
Необработанные исключения наружу уходят как `ERR-PLATFORM-001` без деталей;
ошибки валидации — `ERR-PLATFORM-002` с `details`; 404/405 и прочие
`HTTPException` фреймворка — `ERR-PLATFORM-003`. `X-Correlation-ID` есть
в каждом ответе, включая 500.

**Работа с БД (§6, §9):**

```python
from hospitality.shared.db import session_scope

async with session_scope() as session:
    session.add(tenant)          # commit при выходе, rollback при исключении
```

`session_scope()` — единственный способ получить сессию; ручной engine/commit
запрещён: контекст тенанта (Task 0009, `tenancy.py`) ставится здесь через
`SET LOCAL` — обход паттерна станет дырой в изоляции. Модели наследуют `Base`;
колонки времени — только тип `UTCDateTime` (наивный datetime падает на
записи), «сейчас» — `utc_now()`. Схема БД меняется только миграциями:
`alembic revision --rev-id NNNN -m "slug" --autogenerate`, применение —
`make migrate`; CI проверяет применимость на чистый Postgres, обратимость
и отсутствие дрейфа моделей от миграций (`alembic check`).

**Публиковать доменное событие (Task 0010, P-6, ADR-005):**

```python
from hospitality.shared.events import publish

with tenant_context(tenant_id):
    async with session_scope() as session:
        session.add(service_request)              # бизнес-запись
        await publish(session, RequestCreated(...))  # та же транзакция
```

Событие — наследник `DomainEvent` с `event_name` (канон `<сущность>.<факт>`).
Публикация требует активный `tenant_context`; событие коммитится атомарно
с бизнес-записью (откат транзакции откатывает и его) и уходит в таблицу
`outbox_events`. Отдельный процесс `hospitality.worker` читает outbox и
вызывает подписчиков — доставка **at-least-once**, каждый подписчик обязан
быть идемпотентным (P-8). Подписка — `subscribe(EventType, handler)`,
регистрируется composition root'ом воркера (`hospitality/worker.py`), не
самими модулями. Канонический пример события и идемпотентного подписчика —
`hospitality/platform/events.py`. Настройки цикла воркера (период опроса,
размер пачки, предел попыток доставки) — `worker_poll_interval_seconds`,
`worker_batch_size`, `worker_max_delivery_attempts` в `Settings`.

**Backoff и retention outbox (ADR-009):** неудачная доставка откладывает
следующую попытку того же события на `next_attempt_at` — экспоненциально,
`worker_retry_backoff_base_seconds` → `..._max_seconds`; диспетчер не берёт
строку в работу раньше этого момента (при исчерпании
`worker_max_delivery_attempts` — см. ERR-EVENTS-002 в
`docs/runbooks/errors.md`, ручное восстановление обязано сбросить и
`next_attempt_at`, не только `attempts`). Строки с `processed_at` старше
`outbox_retention_days` (по умолчанию 30) периодически удаляет
`cleanup_processed_events()` — вызывается из `run_worker()` на старте
процесса и дальше раз в `worker_cleanup_interval_seconds` (по умолчанию час),
отдельная джоба не заводится (NG-8).

**Rate-limit / счётчик в Redis (issue #41, spec 0023, §6):**

```python
from hospitality.shared.ratelimit import consume_rate_limit

decision = await consume_rate_limit(
    scope="guest_chat_window",       # имя лимита — часть ключа, логов и метрик
    key=f"{tenant_id}:{chat_id}",    # чей лимит; tenant_id обязателен (P-4)
    limit=20,
    window_seconds=600,
)
if not decision.allowed:
    ...  # отказ без дорогой работы; ответить один раз — decision.first_rejection
```

Единственный канон rate-limit'а и работы с Redis в бизнес-коде (P-12).
Fixed-window: номер окна в ключе, смена окна сама обнуляет счёт; всплеск до
2N на границе окон — принятый компромисс (spec 0023). **Fail-open:** Redis
недоступен → `allowed=True, available=False` + WARNING
`rate_limit_backend_unavailable` — отказ в обслуживании хуже перерасхода,
потолок расхода держит бюджет LLM (ERR-AI-002). У Redis нет RLS — изоляция
тенантов держится дисциплиной ключей (`key` начинается с `tenant_id`).
Клиент — `create_redis_client()` (из настроек, на один вызов: пул redis-py
привязан к event loop'у создания, синглтон ломался бы в тестах — по loop'у на
тест); прямое создание клиентов в остальном бизнес-коде запрещено (диагностика
`health.py` — отдельный путь).
Первый потребитель — общий ход гостя (`channels/common/guest_turn.py`, две
ступени, ERR-CHANNEL-003); значения лимитов — `GUEST_CHAT_RATE_LIMIT_*` в
`Settings`.

**Наблюдаемость (Task 0018, §10.4, §10.7):**

Sentry и метрики подключены инфраструктурно — новому коду обычно НЕ нужно
звать их напрямую: необработанные исключения и ERROR-логи уходят в Sentry
сами (с тэгами `tenant_id`/`correlation_id` из contextvars structlog),
HTTP-запросы учитываются в RED-метриках `CorrelationIdMiddleware`, вызовы LLM —
внутри gateway. Новая метрика = объявление в `metrics.py` + функция записи
рядом с существующими (`record_http_request`, `record_llm_call`) + вызов из
инфраструктурной точки (не из бизнес-логики модулей). `GET /metrics` анонимен —
явное решение §11 (симметрично `/health`): PII и секретов в метриках нет.
Алерты по метрикам шлёт `hospitality.tools.alerter`
(docs/runbooks/alerts.md); OTel — заготовка, полный трейсинг Phase 1.

## Типовые сценарии изменения

- Новая настройка окружения → поле в `Settings` + строка в `.env.example`.
- Новая таблица → модель от `Base` в `models.py` своего модуля + импорт модуля
  в `alembic/env.py` + `alembic revision --rev-id NNNN --autogenerate` + тесты.
- Новый код ошибки → константа рядом с местом использования + статья в
  `docs/runbooks/errors.md` в том же PR.
- Новое обязательное поле лога → процессор в `logging.py` + обновить §10.1-список здесь
  и тест обязательных полей.
- Новое доменное событие → класс `DomainEvent` в `events.py` своего модуля
  (копия канона `platform/events.py`) + подписчик там же, если нужен +
  регистрация пары в `hospitality/worker.py`.

## Известные отступления (зафиксировано, §12)

- `/openapi.json` описывает 422 дефолтной схемой FastAPI (`{"detail": [...]}`),
  фактический ответ — конверт `ERR-PLATFORM-002`. Потребителей спеки в Фазе 0 нет;
  выправить схему — при первом генерируемом по спеке клиенте.
- Автоматического маскирования PII в логах (§10.1) ещё нет; сейчас сырой ввод клиента
  в логи просто не пишется (см. `_handle_validation_error`). Полноценное маскирование —
  отдельной задачей (#13); основа уже есть — `pii.py` (spec 0031).
- `/metrics` анонимен (явное решение §11, Task 0018) — пересмотреть при выходе
  в прод (reverse-proxy / allowlist / токен).
- Заголовки Sentry-событий из ERROR-логов выглядят как сериализованный
  event-dict structlog (косметика ProcessorFormatter); группировка корректна —
  по exc_info. Причёсывать — только если начнёт мешать.

## Зависимости

Внешние: fastapi/starlette, pydantic, structlog, asyncpg, redis,
sqlalchemy (async), alembic, sentry-sdk, prometheus-client.
Внутренние: только внутри `shared` (`errors` → `middleware` → `metrics` →
`events`/`db`; `middleware` → `logging`; `db` → `config`; `config` ни от чего
не зависит — уровень логирования в `configure_logging` передаёт composition
root `app.py`).

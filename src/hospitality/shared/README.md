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
Необработанные исключения наружу уходят как `ERR-PLATFORM-001` без деталей.

## Типовые сценарии изменения

- Новая настройка окружения → поле в `Settings` + строка в `.env.example`.
- Новый код ошибки → константа рядом с местом использования + статья в
  `docs/runbooks/errors.md` в том же PR.
- Новое обязательное поле лога → процессор в `logging.py` + обновить §10.1-список здесь
  и тест обязательных полей.

## Зависимости

Внешние: fastapi/starlette, pydantic, structlog, asyncpg, redis.
Внутренние: только внутри `shared` (`errors` → `middleware` → `logging` → `config`).

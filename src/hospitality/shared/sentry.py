"""Sentry — сбор необработанных ошибок (Task 0018, FOUNDATION §10.4, §10.12).

``init_sentry()`` вызывается из composition root'ов обоих процессов —
``app.py`` (``create_app``) и ``worker.py`` (``main``) — до сборки приложения.
Пустой ``SENTRY_DSN`` — Sentry выключен (канон «пустой секрет валиден», как
``ANTHROPIC_API_KEY``): dev/CI работают без внешнего сервиса.

Контекст события (§10.4 — «тенант, correlation id, модуль»): ``before_send``
переносит ``tenant_id`` и ``correlation_id`` из contextvars structlog в тэги
события. Один механизм покрывает оба процесса: в HTTP-запросе contextvars
биндят ``CorrelationIdMiddleware``/``TenantContextMiddleware`` (и намеренно
не снимают до конца запроса — см. docstring ``TenantContextMiddleware``),
в воркере — ``tenant_context()`` на каждое событие.

Что попадает в Sentry:

- необработанные исключения HTTP-процесса (интеграции Starlette/FastAPI,
  включаются автоматически) и падения процессов (``excepthook``);
- записи логов уровня ERROR (``LoggingIntegration``) — так ловятся
  «пойманные, но ненормальные» ошибки воркера (``worker_iteration_failed``
  и т.п.); дубль «исключение + его же ERROR-лог» схлопывает штатная
  ``DedupeIntegration``.

Ожидаемые ``AppError`` логируются на WARNING (``shared/errors.py``) и событий
не порождают — их диагностирует каталог ошибок (§10.5), а не трекер.
PII: ``send_default_pii`` остаётся False (умолчание SDK), тела запросов не
отправляются; трейсинг производительности не включается (OTel — Phase 1).
"""

from __future__ import annotations

import sentry_sdk
import structlog
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint

from hospitality.shared.config import Settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Поля contextvars structlog (§10.1), которые становятся тэгами события.
_CONTEXT_TAG_FIELDS = ("tenant_id", "correlation_id")


def add_context_tags(event: Event, _hint: Hint) -> Event:
    """``before_send``: тэги tenant_id/correlation_id из контекста логирования."""
    context = structlog.contextvars.get_contextvars()
    tags = event.setdefault("tags", {})
    for field in _CONTEXT_TAG_FIELDS:
        value = context.get(field)
        if value is not None and field not in tags:
            tags[field] = value
    return event


def init_sentry(settings: Settings, *, transport: Transport | None = None) -> None:
    """Инициализировать Sentry процесса. ``transport`` переопределяют только
    тесты (in-memory перехват событий вместо сети)."""
    if not settings.sentry_dsn:
        logger.info("sentry_disabled")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        before_send=add_context_tags,
        transport=transport,
    )
    logger.info("sentry_enabled", environment=settings.sentry_environment)

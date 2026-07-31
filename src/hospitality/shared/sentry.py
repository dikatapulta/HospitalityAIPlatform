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
Секреты: ``send_default_pii=False`` режет куки, тело и IP, но НЕ путь URL —
секретные сегменты пути маскирует ``before_send``
(``redact_secrets_in_path``, §11) в четырёх местах, куда путь попадает: сам
URL, ``query_string``, заголовки (``referer`` браузер шлёт с полным адресом
страницы, и в список чувствительных заголовков SDK он не входит) и имя
транзакции (до роутинга интеграция берёт его из сырого URL). Локальные
переменные фреймов не отправляются вовсе — см. ``include_local_variables``
в ``init_sentry``.
"""

from __future__ import annotations

import sentry_sdk
import structlog
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint

from hospitality.shared.config import Settings
from hospitality.shared.logging import get_logger, redact_secrets_in_path

logger = get_logger(module=__name__)

# Поля contextvars structlog (§10.1), которые становятся тэгами события.
_CONTEXT_TAG_FIELDS = ("tenant_id", "correlation_id")


def add_context_tags(event: Event, _hint: Hint) -> Event:
    """``before_send``: тэги tenant_id/correlation_id + маскирование секретов в URL."""
    context = structlog.contextvars.get_contextvars()
    tags = event.setdefault("tags", {})
    for field in _CONTEXT_TAG_FIELDS:
        value = context.get(field)
        if value is not None and field not in tags:
            tags[field] = value
    _redact_request_secrets(event)
    # Имя транзакции: пока роутер не положил маршрут в ASGI-scope, интеграция
    # берёт его из сырого URL (`transaction_info.source = "url"`) — падение в
    # middleware отдало бы живой токен bind-ссылки. На маршрутном событии имя —
    # уже шаблон, и маскирование делает из `/w/{tenant_slug}/b/{token}`
    # `/w/{tenant_slug}/b/***`: константа, соответствие маршруту 1:1 сохраняется.
    transaction = event.get("transaction")
    if isinstance(transaction, str):
        event["transaction"] = redact_secrets_in_path(transaction)
    return event


def _redact_request_secrets(event: Event) -> None:
    """Замаскировать секреты пути в описании запроса: URL, query и заголовки.

    Описание запроса кладёт интеграция Starlette, и `send_default_pii=False`
    его почти не трогает — секрет в пути (токен bind-ссылки) уехал бы в трекер
    как есть. Заголовки чистятся не для галочки: SDK вырезает свой список
    (cookie, authorization, x-real-ip …), где `referer` НЕТ, а браузер шлёт в
    нём полный адрес страницы — гость, открывший bind-ссылку, отдаёт токен и в
    запросе согласия, и при переходе по ссылке на политику (ревью PR #155).
    Логи-breadcrumbs чистит сам access-log (`CorrelationIdMiddleware`).
    """
    request = event.get("request")
    if not isinstance(request, dict):
        return
    for field in ("url", "query_string"):
        value = request.get(field)
        if isinstance(value, str):
            request[field] = redact_secrets_in_path(value)
    headers = request.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            # Вырезанные SDK заголовки — не строки (AnnotatedValue), их не трогаем.
            if isinstance(value, str):
                headers[name] = redact_secrets_in_path(value)


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
        # Локальные переменные фреймов НЕ отправляем (умолчание SDK — отправлять).
        # В стектрейсе каждого ASGI-фрейма лежит `scope` с сырым путём запроса, а у
        # обработчика — path-параметр `token` вложенным словарём; штатный
        # EventScrubber нерекурсивен и до него не достаёт, а маскирование по форме
        # пути бессильно — там токен лежит голым значением (ревью PR #155). Причина
        # падения видна по трейсбеку и структурным логам, значения переменных того
        # не стоят: любой секрет, доехавший до фрейма, уехал бы в трекер. Опция
        # глобальная: локалов лишаются события ОБОИХ процессов, в том числе
        # ERROR-логи воркера (`worker_iteration_failed` и т.п.).
        include_local_variables=False,
        transport=transport,
    )
    logger.info("sentry_enabled", environment=settings.sentry_environment)

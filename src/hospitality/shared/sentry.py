"""Sentry — сбор необработанных ошибок (Task 0018, FOUNDATION §10.4, §10.12).

``init_sentry()`` вызывается из composition root'ов процессов —
``app.py`` (``create_app``), ``worker.py`` и ``tools/alerter.py`` (``main``,
алертер — с issue #229) — до сборки приложения.
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
Трейсинг производительности не включается (OTel — Phase 1).

PII: ``send_default_pii`` остаётся False (умолчание SDK) — это режет куки,
заголовки с креденшелами и IP, но НЕ тело запроса: его интеграция Starlette
кладёт в ``request.data`` каждого события запроса (умолчание SDK — до 10 КБ).
Там сырой текст гостя до маскирования карт и Telegram-update целиком, поэтому
тело не отправляется вовсе — ``max_request_body_size="never"`` (#433).
Локальные переменные фреймов не отправляются тоже — см.
``include_local_variables`` в ``init_sentry``.

Секреты в адресах (§11): ``before_send`` прогоняет ``redact_secrets_in_path``
по КАЖДОЙ строке события, а не по списку полей. Путь со секретом приезжает
многими полями — URL запроса, ``query_string``, заголовок ``referer`` (его нет
в списке чувствительных заголовков SDK), имя транзакции (до роутинга оно
берётся из сырого URL), текст исключения и строки логов в хлебных крошках
(токен бота: ``HTTPStatusError`` httpx несёт полный адрес Bot API, #433).
Список полей каждый раз оказывался неполным (ревью PR #155, #433, #485),
обход всего события — нет.

Исходящие запросы: интеграция httpx пишет в хлебные крошки полный адрес
каждого запроса, а в воркере крошки копятся (до 100) и уходят с любым
ERROR. Адрес подписки на веб-пуш — идентификатор телефона сотрудника
(docs/PII_REGISTRY.md) — не секрет известной формы, и маска его не узнает,
поэтому ``before_breadcrumb`` оставляет от адреса только схему и хост
(``strip_http_breadcrumb``, ревью PR #501, Н-4).
"""

from __future__ import annotations

from urllib.parse import urlsplit

import sentry_sdk
import structlog
from sentry_sdk.transport import Transport
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

from hospitality.shared.config import Settings
from hospitality.shared.logging import get_logger, redact_secrets_in_path

logger = get_logger(module=__name__)

# Поля contextvars structlog (§10.1), которые становятся тэгами события.
_CONTEXT_TAG_FIELDS = ("tenant_id", "correlation_id")


def strip_http_breadcrumb(crumb: Breadcrumb, _hint: BreadcrumbHint) -> Breadcrumb:
    """``before_breadcrumb``: от адреса исходящего запроса — только схема и хост."""
    data = crumb.get("data")
    if crumb.get("category") != "httplib" or not isinstance(data, dict):
        return crumb
    url = data.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            data["url"] = f"{parts.scheme}://{parts.hostname or ''}"
        except ValueError:
            data["url"] = ""
    for field in ("http.query", "http.fragment"):
        data.pop(field, None)
    return crumb


def add_context_tags(event: Event, _hint: Hint) -> Event:
    """``before_send``: тэги tenant_id/correlation_id + маскирование секретов во всём событии."""
    context = structlog.contextvars.get_contextvars()
    tags = event.setdefault("tags", {})
    for field in _CONTEXT_TAG_FIELDS:
        value = context.get(field)
        if value is not None and field not in tags:
            tags[field] = value
    _redact_secrets_in_strings(event)
    return event


def _redact_secrets_in_strings(node: object) -> object:
    """Замаскировать секреты в каждой строке вложенной структуры, на месте.

    До ``before_send`` SDK уже сериализовал событие: внутри только словари,
    списки и скаляры. Ключи словарей не трогаем — секрет живёт в значениях.
    Шаблон маршрута в имени транзакции тоже проходит через маску:
    `/w/{tenant_slug}/b/***` — константа, соответствие маршруту 1:1 сохраняется.
    """
    if isinstance(node, str):
        return redact_secrets_in_path(node)
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = _redact_secrets_in_strings(value)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            node[index] = _redact_secrets_in_strings(value)
    elif isinstance(node, tuple):
        return [_redact_secrets_in_strings(value) for value in node]
    return node


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
        before_breadcrumb=strip_http_breadcrumb,
        # Тело запроса не отправляем (умолчание SDK — до 10 КБ): у гостя там
        # сырой текст сообщения, у вебхука Telegram — update с именем гостя (#433).
        max_request_body_size="never",
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

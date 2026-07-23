"""Correlation id для каждого HTTP-запроса (Task 0007, FOUNDATION §10.2).

``CorrelationIdMiddleware`` для каждого запроса:

- берёт correlation id из заголовка ``X-Correlation-ID`` (если формат
  безопасен) или генерирует UUID;
- привязывает его к contextvars structlog — id попадает в каждую лог-запись
  внутри запроса;
- кладёт его в ``request.state.correlation_id`` (для обработчиков ошибок —
  см. ``get_correlation_id``) и в заголовок ответа — клиент всегда может
  сослаться на id при обращении в поддержку;
- пишет каноническую запись ``http_request`` о каждом запросе (метод, путь,
  статус, длительность). Это access-log платформы; access-log uvicorn выключен
  в ``configure_logging`` — у него нет correlation id;
- учитывает запрос в RED-метриках (Task 0018, §10.7): та же точка, что и
  access-log, — один канонический след запроса в логах и метриках (P-12).

Класс — чистый ASGI-middleware, а не ``BaseHTTPMiddleware``: тот выполняет
приложение в отдельной asyncio-задаче, из-за чего привязанные contextvars не
видны обработчику необработанных исключений (он живёт во внешнем
``ServerErrorMiddleware``). Чистый ASGI работает в одном контексте со всем
стеком.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from fastapi import Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import UNMATCHED_ROUTE, record_http_request

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Защита логов от мусора и инъекций: чужой correlation id принимается, только
# если это короткий безопасный токен (UUID, ULID и т.п.); иначе генерируем свой.
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = get_logger(module=__name__)


def get_correlation_id(request: Request) -> str | None:
    """Correlation id текущего запроса — канонический способ получить его
    в обработчиках ошибок и эндпоинтах (не полагается на contextvars)."""
    correlation_id: str | None = getattr(request.state, "correlation_id", None)
    return correlation_id


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        header_value = Headers(scope=scope).get(CORRELATION_ID_HEADER)
        if header_value is not None and _SAFE_CORRELATION_ID.fullmatch(header_value):
            correlation_id = header_value
        else:
            correlation_id = str(uuid.uuid4())

        scope.setdefault("state", {})["correlation_id"] = correlation_id
        # Новый запрос — чистый контекст логирования: asyncio-контекст может
        # переиспользоваться, поля прошлого запроса не должны утекать в текущий.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        # Если приложение упало до отправки ответа, наружу уйдёт 500 —
        # его и фиксируем в http_request по умолчанию.
        status_code = 500
        started_at = time.perf_counter()

        async def send_with_correlation_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self._app(scope, receive, send_with_correlation_id)
        finally:
            duration_seconds = time.perf_counter() - started_at
            duration_ms = round(duration_seconds * 1000, 1)
            logger.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=duration_ms,
            )
            # Лейбл route — шаблон маршрута, не сырой путь (кардинальность,
            # см. shared/metrics.py). Роутер кладёт совпавший маршрут в scope;
            # немэтчнутые запросы (404 сканеров) — константа UNMATCHED_ROUTE.
            route_template = getattr(scope.get("route"), "path", None) or UNMATCHED_ROUTE
            record_http_request(
                method=scope["method"],
                route=route_template,
                status_code=status_code,
                duration_seconds=duration_seconds,
            )

"""Канонические классы ошибок и их сериализация в HTTP-ответ (Task 0007,
FOUNDATION §10.5, R-8).

Ожидаемая ошибка бизнес-логики выбрасывается так:

    from hospitality.shared.errors import AppError

    raise AppError(
        code="ERR-REQUESTS-001",
        message="Категория заявки не настроена у тенанта",
        status_code=409,
    )

Каждый код обязан иметь статью в каталоге ошибок ``docs/runbooks/errors.md``
(что значит, вероятные причины, что проверить) — код без статьи не проходит
ревью. ``message`` показывается клиенту: без секретов и внутренних деталей.

Формат ответа одинаков для всех ошибок API (P-7):

    {"error": {"code": "...", "message": "...", "correlation_id": "..."}}

``register_error_handlers`` подключает четыре обработчика: ожидаемые ``AppError``,
HTTP-ошибки фреймворка — 404/405 и любые ``HTTPException`` (ERR-PLATFORM-003),
ошибки валидации запроса (ERR-PLATFORM-002) и необработанные исключения
(ERR-PLATFORM-001 — наружу уходит только код и correlation id, без деталей, §11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from hospitality.shared.logging import get_logger
from hospitality.shared.middleware import CORRELATION_ID_HEADER, get_correlation_id

# Первые коды каталога (docs/runbooks/errors.md).
INTERNAL_ERROR_CODE = "ERR-PLATFORM-001"
VALIDATION_ERROR_CODE = "ERR-PLATFORM-002"
HTTP_ERROR_CODE = "ERR-PLATFORM-003"

_ERROR_CODE_FORMAT = re.compile(r"^ERR-[A-Z0-9]+-\d{3}$")

logger = get_logger(module=__name__)


class AppError(Exception):
    """Базовый класс всех ожидаемых ошибок платформы (R-8)."""

    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        # Неверный формат кода — ошибка программиста, падаем сразу и громко.
        if not _ERROR_CODE_FORMAT.fullmatch(code):
            raise ValueError(
                f"error code must match ERR-<MODULE>-NNN (docs/runbooks/errors.md), got {code!r}"
            )
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    # Заполняется только для ошибок валидации: список нарушений от Pydantic.
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """Канонический конверт ошибки API (P-7): единственный формат ошибок наружу."""

    error: ErrorDetail


def register_error_handlers(app: FastAPI) -> None:
    """Подключить канонические обработчики ошибок (вызывается из composition root)."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unhandled_error)


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: str | None,
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope = ErrorResponse(
        error=ErrorDetail(
            code=code, message=message, correlation_id=correlation_id, details=details
        )
    )
    # Заголовок ставим на самом ответе: обработчик Exception выполняется в
    # ServerErrorMiddleware — снаружи CorrelationIdMiddleware, и его ответ уходит
    # мимо send_with_correlation_id (клиент остался бы без заголовка на 500).
    response_headers = dict(headers) if headers else {}
    if correlation_id is not None:
        response_headers[CORRELATION_ID_HEADER] = correlation_id
    return JSONResponse(
        envelope.model_dump(exclude_none=True),
        status_code=status_code,
        headers=response_headers,
    )


# Обработчики принимают Exception, а не конкретный класс: этого требует
# сигнатура add_exception_handler; фактический тип гарантирован регистрацией.


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    correlation_id = get_correlation_id(request)
    logger.warning(
        "app_error",
        code=exc.code,
        status_code=exc.status_code,
        error_message=exc.message,
    )
    return _error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        correlation_id=correlation_id,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    correlation_id = get_correlation_id(request)
    # Отдельной лог-записи нет: статус и путь уже фиксирует событие http_request.
    # 204/304 не допускают тела — повторяем поведение обработчика Starlette.
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=exc.headers)
    return _error_response(
        status_code=exc.status_code,
        code=HTTP_ERROR_CODE,
        # detail — клиентский текст фреймворка ("Not Found", "Method Not Allowed").
        message=str(exc.detail),
        correlation_id=correlation_id,
        # Заголовки исключения обязаны дойти до клиента: Allow на 405,
        # WWW-Authenticate на будущих 401 и т.п.
        headers=exc.headers,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    correlation_id = get_correlation_id(request)
    # В ctx ошибок Pydantic могут попасть несериализуемые объекты.
    details: list[dict[str, Any]] = jsonable_encoder(exc.errors())
    logger.warning(
        "request_validation_failed",
        # Сырой ввод клиента (поле input) в логи не пишем — может содержать PII
        # (§10.1). Клиенту в ответе details отдаётся целиком: это его же ввод.
        details=[
            {"loc": item.get("loc"), "msg": item.get("msg"), "type": item.get("type")}
            for item in details
        ],
    )
    return _error_response(
        status_code=422,
        code=VALIDATION_ERROR_CODE,
        message="Request validation failed",
        correlation_id=correlation_id,
        details=details,
    )


async def _handle_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = get_correlation_id(request)
    # correlation_id передаём явно: обработчик Exception выполняется в
    # ServerErrorMiddleware — снаружи CorrelationIdMiddleware, поэтому не
    # полагаемся на contextvars (устойчиво к смене порядка middleware).
    logger.exception(
        "unhandled_error",
        correlation_id=correlation_id,
        error_type=type(exc).__name__,
    )
    return _error_response(
        status_code=500,
        code=INTERNAL_ERROR_CODE,
        # Внутренности ошибки наружу не отдаём (§11) — диагноз по correlation id в логах.
        message="Internal server error",
        correlation_id=correlation_id,
    )

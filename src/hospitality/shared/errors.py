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

``register_error_handlers`` подключает пять обработчиков: ожидаемые ``AppError``,
HTTP-ошибки фреймворка — 404/405 и любые ``HTTPException`` (ERR-PLATFORM-003),
тело больше предела (ERR-PLATFORM-011, ``shared/body_limit.py``), ошибки валидации
запроса (ERR-PLATFORM-002) и необработанные исключения
(ERR-PLATFORM-001 — наружу уходит только код и correlation id, без деталей, §11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from hospitality.shared.body_limit import BODY_TOO_LARGE_ERROR_CODE, RequestBodyTooLargeError
from hospitality.shared.logging import get_logger
from hospitality.shared.middleware import CORRELATION_ID_HEADER, get_correlation_id

# Первые коды каталога (docs/runbooks/errors.md).
INTERNAL_ERROR_CODE = "ERR-PLATFORM-001"
VALIDATION_ERROR_CODE = "ERR-PLATFORM-002"
HTTP_ERROR_CODE = "ERR-PLATFORM-003"

# Нарушений валидации в ответ и в лог — не больше этого; имена полей и тексты — усечены.
# Без потолка тело из тысяч лишних ключей давало строку лога и ответ в десятки мегабайт
# (issue #968): имя каждого ключа — в лог, а ввод клиента — в ответ на каждое нарушение.
MAX_VALIDATION_DETAILS: Final = 20
MAX_LOC_PART_CHARS: Final = 64
_MAX_DETAIL_MSG_CHARS: Final = 256

_ERROR_CODE_FORMAT = re.compile(r"^ERR-[A-Z0-9]+-\d{3}$")

logger = get_logger(module=__name__)


class AppError(Exception):
    """Базовый класс всех ожидаемых ошибок платформы (R-8).

    ``headers`` — заголовки, которые обязаны дойти до клиента вместе с ошибкой
    (например, ``WWW-Authenticate: Bearer`` на 401 — требование RFC 9110 §11.6.1).
    ``details`` — нарушения по частям запроса, когда клиенту нужно показать их у своих
    полей (строки списка приглашений, spec 0072 §4): в ответ уходят как есть, в лог —
    нет, поэтому ни ввода клиента, ни секретов в них не кладут.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        # Неверный формат кода — ошибка программиста, падаем сразу и громко.
        if not _ERROR_CODE_FORMAT.fullmatch(code):
            raise ValueError(
                f"error code must match ERR-<MODULE>-NNN (docs/runbooks/errors.md), got {code!r}"
            )
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers
        self.details = details


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    # Нарушения по частям запроса: от Pydantic у ошибок валидации, у AppError —
    # когда их передал код (`AppError.details`).
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """Канонический конверт ошибки API (P-7): единственный формат ошибок наружу."""

    error: ErrorDetail


def register_error_handlers(app: FastAPI) -> None:
    """Подключить канонические обработчики ошибок (вызывается из composition root)."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    # Подкласс HTTPException: Starlette ищет обработчик по MRO, свой — раньше общего.
    app.add_exception_handler(RequestBodyTooLargeError, _handle_body_too_large)
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
        details=exc.details,
        headers=exc.headers,
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


async def _handle_body_too_large(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestBodyTooLargeError)
    logger.warning(
        "request_body_too_large",
        error_code=BODY_TOO_LARGE_ERROR_CODE,
        limit_bytes=exc.limit_bytes,
        content_length=exc.content_length,
    )
    return _error_response(
        status_code=413,
        code=BODY_TOO_LARGE_ERROR_CODE,
        message="Request body is too large",
        correlation_id=get_correlation_id(request),
    )


def clip_loc(loc: Sequence[Any]) -> list[str | int]:
    """Путь поля из нарушения Pydantic: имена полей — не длиннее `MAX_LOC_PART_CHARS`.

    Имя лишнего ключа — ввод клиента: без усечения ключ в мегабайт целиком лёг бы в лог.
    """
    return [part if isinstance(part, int) else str(part)[:MAX_LOC_PART_CHARS] for part in loc]


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    correlation_id = get_correlation_id(request)
    errors = exc.errors()
    # В ответ и в лог — одно и то же: путь, текст и тип, без ввода клиента (поле input).
    # В логе ввод — PII (§10.1); в ответе — усилитель: у пропущенного поля input — всё тело.
    details: list[dict[str, Any]] = [
        {
            "loc": clip_loc(item.get("loc", ())),
            "msg": str(item.get("msg", ""))[:_MAX_DETAIL_MSG_CHARS],
            "type": str(item.get("type", "")),
        }
        for item in errors[:MAX_VALIDATION_DETAILS]
    ]
    logger.warning("request_validation_failed", error_count=len(errors), details=details)
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

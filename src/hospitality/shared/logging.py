"""Канонический логгер платформы (Task 0007, FOUNDATION §10.1–10.2, P-10, P-12).

Единственный способ логировать в проекте:

    from hospitality.shared.logging import get_logger

    logger = get_logger(module=__name__)
    logger.info("guest_checked_in", guest_id=guest_id)

Каждая запись — одна строка JSON в stdout с обязательными полями §10.1:
``timestamp``, ``level``, ``tenant_id``, ``correlation_id``, ``trace_id``,
``module``, ``event`` плюс произвольный контекст. ``correlation_id`` попадает
в записи автоматически из contextvars, привязанных CorrelationIdMiddleware;
``tenant_id`` начнёт заполняться контекстом тенанта (Task 0009), ``trace_id`` —
трассировкой OpenTelemetry (§10.3). До тех пор поля равны null: схема записи
зафиксирована сразу, чтобы не менять формат задним числом.

``configure_logging()`` перенастраивает и стандартный ``logging``, поэтому логи
uvicorn и сторонних библиотек выходят тем же JSON через тот же обработчик.
Access-log uvicorn выключен: канонический след запроса — событие
``http_request`` из CorrelationIdMiddleware (в нём есть correlation id,
у uvicorn — нет).
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger

# Поля §10.1, обязанные присутствовать в каждой записи — в том числе вне
# HTTP-контекста (старт приложения, фоновая задача, тест), где contextvars пусты.
_REQUIRED_CONTEXT_FIELDS = ("tenant_id", "correlation_id", "trace_id", "module")


def _ensure_required_fields(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    for field in _REQUIRED_CONTEXT_FIELDS:
        event_dict.setdefault(field, None)
    return event_dict


def _add_module_from_stdlib_record(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    # Логи сторонних библиотек идут через stdlib logging и не знают про наше
    # поле module — берём его из имени stdlib-логгера ("uvicorn.error" и т.п.).
    record: logging.LogRecord | None = event_dict.get("_record")
    if record is not None:
        event_dict.setdefault("module", record.name)
    return event_dict


# Общая часть конвейера structlog-логов и логов сторонних библиотек:
# одинаковые записи независимо от источника (P-12).
_shared_processors: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    _add_module_from_stdlib_record,
    _ensure_required_fields,
]


def configure_logging(level: str = "INFO") -> None:
    """Настроить JSON-логирование процесса. Идемпотентно: повторный вызов
    заменяет конфигурацию, а не дублирует обработчики."""
    structlog.configure(
        processors=[
            *_shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level.upper())

    # Логи uvicorn — через общий корневой обработчик (тот же JSON).
    for logger_name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # Access-log uvicorn выключен: его заменяет событие http_request
    # из CorrelationIdMiddleware (см. docstring модуля).
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    """Канонический способ получить логгер: ``get_logger(module=__name__)``."""
    return structlog.stdlib.get_logger(module, module=module)

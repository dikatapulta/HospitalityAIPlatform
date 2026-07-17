"""Реестр AI-инструментов (Task 0015, §7.3).

Единственное место, где оркестратор берёт: (1) `ToolSpec`-ы под текущего
тенанта, (2) класс подтверждения инструмента (P-9), (3) диспетчер исполнения.
Новый инструмент = запись здесь + модуль-обёртка (канон `create_service_request`).
"""

from __future__ import annotations

from typing import Any

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools import create_service_request as _create_service_request
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError

# Классы подтверждения по имени инструмента (P-9).
_CONFIRMATION_CLASSES: dict[str, ConfirmationClass] = {
    _create_service_request.NAME: _create_service_request.CONFIRMATION_CLASS,
}


async def build_tool_specs() -> list[ToolSpec]:
    """Собрать инструменты под текущего тенанта (категории — из его конфига)."""
    categories = await requests_api.list_categories()
    category_keys = [category.key for category in categories]
    return [_create_service_request.build_spec(category_keys)]


def confirmation_class(tool_name: str) -> ConfirmationClass:
    """Класс подтверждения инструмента (P-9). Неизвестный инструмент — ERR-AI-004."""
    try:
        return _CONFIRMATION_CLASSES[tool_name]
    except KeyError as error:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message=f"unknown tool {tool_name!r}",
            status_code=422,
        ) from error


async def execute(tool_name: str, arguments: dict[str, Any]) -> requests_api.ServiceRequestRead:
    """Исполнить инструмент по имени (внутри `tenant_context`, P-4)."""
    if tool_name == _create_service_request.NAME:
        return await _create_service_request.execute(arguments)
    raise AppError(
        code=ERR_AI_INVALID_TOOL_CALL,
        message=f"unknown tool {tool_name!r}",
        status_code=422,
    )

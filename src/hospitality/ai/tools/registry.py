"""Реестр AI-инструментов (Task 0015, §7.3; spec 0025).

Единственное место, где оркестратор берёт: (1) `ToolSpec`-ы под текущего
тенанта и текущий ход, (2) класс подтверждения инструмента (P-9), (3) диспетчер
исполнения. Новый инструмент = модуль-обёртка (канон `create_service_request`)
+ запись в `_TOOLS` + строка в `build_tool_specs`.

Контракт модуля инструмента: `NAME`, `CONFIRMATION_CLASS`, `CREATES_REQUEST`
(создаёт ли заявку — по нему оркестратор заполняет `created_request_id` хода),
`build_spec(...)`, `execute(arguments, context)`.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools import cancel_service_request as _cancel_service_request
from hospitality.ai.tools import create_service_request as _create_service_request
from hospitality.ai.tools.base import ConfirmationClass, ToolTurnContext
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError

# Имя инструмента → модуль-обёртка. Единственный источник состава инструментов.
_TOOLS: dict[str, ModuleType] = {
    _create_service_request.NAME: _create_service_request,
    _cancel_service_request.NAME: _cancel_service_request,
}


async def build_tool_specs(context: ToolTurnContext) -> list[ToolSpec]:
    """Собрать инструменты под текущего тенанта и текущий ход (§7.4, spec 0025).

    `create_service_request` — всегда (enum категорий из конфига тенанта);
    `cancel_service_request` — только когда у диалога есть открытые заявки:
    пустой enum допустимых id бессмыслен и провоцирует галлюцинации.
    """
    categories = await requests_api.list_categories()
    category_keys = [category.key for category in categories]
    specs = [_create_service_request.build_spec(category_keys)]
    if context.active_requests:
        specs.append(_cancel_service_request.build_spec(context.active_requests))
    return specs


def confirmation_class(tool_name: str) -> ConfirmationClass:
    """Класс подтверждения инструмента (P-9). Неизвестный инструмент — ERR-AI-004."""
    module = _tool_module(tool_name)
    result: ConfirmationClass = module.CONFIRMATION_CLASS
    return result


def creates_request(tool_name: str) -> bool:
    """Создаёт ли инструмент заявку (spec 0025): по этому флагу оркестратор
    заполняет `created_request_id`, а канал — привязку `request_origins`."""
    module = _tool_module(tool_name)
    result: bool = module.CREATES_REQUEST
    return result


def done_text(tool_name: str) -> str:
    """Резервная реплика «действие исполнено», когда модель не дала текста.

    У каждого инструмента своя (spec 0025): «передаю в службу» для создания
    было бы ложью для отмены. В норме реплику даёт классификатор гейта на
    языке гостя — это последний рубеж.
    """
    module = _tool_module(tool_name)
    result: str = module.DONE_TEXT
    return result


async def execute(
    tool_name: str, arguments: dict[str, Any], context: ToolTurnContext
) -> requests_api.ServiceRequestRead:
    """Исполнить инструмент по имени (внутри `tenant_context`, P-4)."""
    module = _tool_module(tool_name)
    result: requests_api.ServiceRequestRead = await module.execute(arguments, context)
    return result


def _tool_module(tool_name: str) -> ModuleType:
    try:
        return _TOOLS[tool_name]
    except KeyError as error:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message=f"unknown tool {tool_name!r}",
            status_code=422,
        ) from error

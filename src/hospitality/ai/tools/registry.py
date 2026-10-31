"""Реестр AI-инструментов (Task 0015, §7.3; spec 0025).

Единственное место, где оркестратор берёт: (1) `ToolSpec`-ы под текущего
тенанта и текущий ход, (2) класс подтверждения инструмента (P-9), (3) диспетчер
исполнения. Новый инструмент = модуль-обёртка (канон `create_service_request`)
+ запись в `_TOOLS` + строка в `build_tool_specs`.

Контракт модуля инструмента: `NAME`, `CONFIRMATION_CLASS`, `CREATES_REQUEST`
(создаёт ли заявку — по нему оркестратор заполняет `created_request_id` хода),
`build_spec(...)`, `prepare(arguments, context)` (вызов модели → вызов для
гейта P-9, spec 0056 §4), `execute(arguments, context)`,
`confirmation_waived(arguments)` (снят ли гейт P-9 на этом вызове — ADR-018),
`done_text(arguments)` (резервная реплика «исполнено») и
`done_text_is_final(arguments)` (реплика инструмента важнее реплики
классификатора). Функции обязательны у каждого инструмента, а не
опциональны: `getattr`-магия «а вдруг модуль её объявил» — ровно тот скрытый
контракт, который запрещает P-1.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools import cancel_service_request as _cancel_service_request
from hospitality.ai.tools import create_service_request as _create_service_request
from hospitality.ai.tools import escalate_to_staff as _escalate_to_staff
from hospitality.ai.tools.base import (
    ConfirmationClass,
    PreparedCall,
    ToolTurnContext,
    without_code_only_arguments,
)
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Имя инструмента → модуль-обёртка. Единственный источник состава исполняемых
# инструментов. `escalate_to_staff` здесь нет: исполнения в ai/ у него нет, его
# эффект — эскалация канала, и оркестратор перехватывает вызов до диспетчера.
_TOOLS: dict[str, ModuleType] = {
    _create_service_request.NAME: _create_service_request,
    _cancel_service_request.NAME: _cancel_service_request,
}


async def build_tool_specs(context: ToolTurnContext, config: TenantConfig | None) -> list[ToolSpec]:
    """Собрать инструменты под текущего тенанта и текущий ход (§7.4, spec 0025).

    `create_service_request` — при включённом `ENABLE_SERVICE_REQUESTS` (enum
    категорий тенанта + подсказки служб из его конфига, issue #123);
    `cancel_service_request` — только когда у диалога есть открытые заявки:
    пустой enum допустимых id бессмыслен и провоцирует галлюцинации.
    `escalate_to_staff` (spec 0040) — тем же флагом, что и создание: флаг
    включают в день этапа 3 вместе со службой эскалаций, а до того позвать
    некого — у гостевой копии без бота эскалация уходит в никуда, и вызов
    инструмента был бы тем же обещанием без эффекта, что и слова (#101).

    Режим «только консультации» (`ENABLE_SERVICE_REQUESTS=false`, умолчание)
    выключает создание заявки именно ЗДЕСЬ, а не проверкой внутри инструмента:
    флаг обязан убрать инструмент из запроса к провайдеру целиком, чтобы модель
    о нём не знала и не предлагала гостю действий, которых не совершит.
    Парный ему блок системного промпта — `orchestrator._consultation_only_block`.
    Отмена остаётся: заявки, созданные до выключения флага (или персоналом),
    гость вправе отменить.

    `config` читает и передаёт оркестратор — один раз на ход (spec 0036 §4):
    тот же конфиг несёт справочник отеля в системный промпт, и второе чтение
    здесь было бы вторым одинаковым запросом к БД на каждую реплику гостя.
    `None` — конфиг недоступен (онбординг не завершён или дрейф схемы):
    инструмент собирается без подсказок, как до issue #123.
    """
    specs: list[ToolSpec] = []
    if get_settings().enable_service_requests:
        categories = await requests_api.list_categories()
        category_keys = [category.key for category in categories]
        hints = {} if config is None else config.category_hints
        quotas = () if config is None else config.service_quotas
        specs.append(_create_service_request.build_spec(category_keys, hints, quotas))
        specs.append(_escalate_to_staff.build_spec())
    if context.active_requests:
        specs.append(_cancel_service_request.build_spec(context.active_requests))
    return specs


def confirmation_class(tool_name: str) -> ConfirmationClass:
    """Класс подтверждения инструмента (P-9). Неизвестный инструмент — ERR-AI-004.

    Свойство КОНТРАКТА инструмента и от аргументов вызова не зависит — так
    сказано в P-9. Снятие гейта на конкретном вызове — отдельный вопрос и
    отдельная функция ниже (ADR-018).
    """
    module = _tool_module(tool_name)
    result: ConfirmationClass = module.CONFIRMATION_CLASS
    return result


def confirmation_waived(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Удовлетворён ли гейт P-9 уже самим сообщением гостя (ADR-018, spec 0034).

    True — оркестратор исполняет инструмент сразу, не переспрашивая. Сегодня
    это ровно один случай: срочная заявка (`is_urgent`), где вопрос «оформить?»
    требовал бы подтвердить то, что гость уже сказал прямым текстом. Класс
    инструмента при этом не меняется (см. выше).
    """
    module = _tool_module(tool_name)
    result: bool = module.confirmation_waived(arguments)
    return result


async def prepare(
    tool_name: str, arguments: dict[str, Any], context: ToolTurnContext
) -> PreparedCall:
    """Вызов модели → вызов для гейта P-9 (spec 0056 §4); неизвестный — ERR-AI-004.

    Оркестратор зовёт его у каждого вызова модели, после проверки имени и до
    гейта — и у срочного вызова, где гейт снят. Ключи, которые пишет только
    код (`CODE_ONLY_ARGUMENTS`), выбрасываются здесь, до инструмента: защита не
    зависит от того, не забыл ли её `prepare` нового инструмента.
    """
    module = _tool_module(tool_name)
    result: PreparedCall = await module.prepare(without_code_only_arguments(arguments), context)
    return result


def done_text_is_final(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Отдать гостю `done_text`, а не реплику классификатора гейта (spec 0056 Р-7).

    Сегодня — у платной заявки сверх квоты: цену гостю называет только код.
    """
    module = _tool_module(tool_name)
    result: bool = module.done_text_is_final(arguments)
    return result


def creates_request(tool_name: str) -> bool:
    """Создаёт ли инструмент заявку (spec 0025): по этому флагу оркестратор
    заполняет `created_request_id`, а канал — привязку `request_origins`."""
    module = _tool_module(tool_name)
    result: bool = module.CREATES_REQUEST
    return result


def done_text(tool_name: str, arguments: dict[str, Any]) -> str:
    """Реплика «действие исполнено» от самого инструмента, под аргументы вызова.

    У каждого инструмента своя (spec 0025): «передаю в службу» для создания
    было бы ложью для отмены. Обычно это последний рубеж — реплику даёт
    классификатор гейта на языке гостя; на снятом гейте (ADR-018) рубежей
    больше нет, и текст инструмента — единственный, что услышит гость
    (spec 0034 §5), поэтому он зависит от аргументов, а не константа.
    """
    module = _tool_module(tool_name)
    result: str = module.done_text(arguments)
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

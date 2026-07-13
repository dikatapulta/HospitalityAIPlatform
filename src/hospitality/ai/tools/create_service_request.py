"""CANONICAL инструмент AI: создать заявку службе отеля (Task 0015, P-5, §7.3).

Эталон паттерна «инструмент = тонкая обёртка над сервисом ядра». Копируется
всеми будущими инструментами. Логики нет — только контракт и обёртка над
`modules/requests`:

- вход для модели — `category_key` (slug), а НЕ `category_id` (UUID): модель не
  знает и не должна выдумывать внутренний UUID (§7.4, анти-галлюцинация).
  Оркестратор кладёт в схему `enum` реальных ключей тенанта; обёртка резолвит
  key→id тенантной сессией;
- класс подтверждения — `confirm_guest` (P-9): заявку создаёт гость, гейт
  исполнения — на оркестраторе.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError

NAME = "create_service_request"
CONFIRMATION_CLASS = ConfirmationClass.CONFIRM_GUEST

# Код каталога ошибок (docs/runbooks/errors.md, R-8): вызов инструмента моделью
# не соответствует контракту — неизвестный `category_key` (вне enum) или
# невалидные аргументы. Оркестратор превращает это в эскалацию, не в 5xx гостю.
ERR_AI_INVALID_TOOL_CALL = "ERR-AI-004"

_DESCRIPTION = (
    "Оформить заявку одной из служб отеля (уборка, инженерная служба, room "
    "service, IT и т.п.). Вызывай, когда гость просит что-то сделать в номере "
    "или для него. Поле category_key бери ТОЛЬКО из списка допустимых значений "
    "(enum); если ни одно не подходит — не вызывай инструмент, а передай гостя "
    "сотруднику. summary — краткая суть на языке гостя."
)


class CreateServiceRequestArgs(BaseModel):
    """Аргументы, которые модель передаёт инструменту."""

    category_key: str
    summary: str = Field(min_length=1, max_length=500)
    room_number: str | None = Field(default=None, max_length=20)
    details: str | None = Field(default=None, max_length=4000)


def build_spec(category_keys: list[str]) -> ToolSpec:
    """Собрать `ToolSpec` под текущий набор категорий тенанта (§7.4)."""
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "category_key": {
                    "type": "string",
                    "enum": category_keys,
                    "description": "Служба отеля — строго одно из допустимых значений.",
                },
                "summary": {
                    "type": "string",
                    "description": "Краткая суть заявки на языке гостя.",
                },
                "room_number": {
                    "type": "string",
                    "description": "Номер комнаты, если известен.",
                },
                "details": {
                    "type": "string",
                    "description": "Дополнительные детали, если есть.",
                },
            },
            "required": ["category_key", "summary"],
        },
    )


async def execute(arguments: dict[str, Any]) -> requests_api.ServiceRequestRead:
    """Создать заявку из аргументов модели (внутри `tenant_context`, P-4).

    Категории читаются тенантной сессией — key резолвится в id только среди
    категорий этого тенанта (RLS, P-4). Ключ вне списка или невалидные
    аргументы — ERR-AI-004 (модель нарушила контракт).
    """
    try:
        args = CreateServiceRequestArgs.model_validate(arguments)
    except ValidationError as error:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message="create_service_request arguments do not match the tool contract",
            status_code=422,
        ) from error

    categories = await requests_api.list_categories()
    category_id = next((c.id for c in categories if c.key == args.category_key), None)
    if category_id is None:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message=f"unknown request category_key {args.category_key!r}",
            status_code=422,
        )

    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            summary=args.summary,
            room_number=args.room_number,
            details=args.details,
        )
    )

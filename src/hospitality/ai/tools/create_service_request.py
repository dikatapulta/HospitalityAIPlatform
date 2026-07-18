"""CANONICAL инструмент AI: создать заявку службе отеля (Task 0015, P-5, §7.3).

Эталон паттерна «инструмент = тонкая обёртка над сервисом ядра». Копируется
всеми будущими инструментами. Логики нет — только контракт и обёртка над
`modules/requests`:

- вход для модели — `category_key` (slug), а НЕ `category_id` (UUID): модель не
  знает и не должна выдумывать внутренний UUID (§7.4, анти-галлюцинация).
  Оркестратор кладёт в схему `enum` реальных ключей тенанта; обёртка резолвит
  key→id тенантной сессией;
- класс подтверждения — `confirm_guest` (P-9): заявку создаёт гость, гейт
  исполнения — на оркестраторе;
- `confirmation_question` — вопрос-подтверждение гостю на его языке (обязательный
  аргумент): модель почти всегда зовёт инструмент без свободного текста (замер:
  Sonnet и Haiku на 6 языках дают tool_use с пустым `text`), поэтому естественный
  вопрос берём из аргумента. UX-поле гейта: оркестратор показывает его гостю,
  сервис ядра игнорирует (не персистится).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

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

# Язык гостя из аргумента модели: «kk», «KK», «kk-KZ» → «kk»; мусор → None.
# Терпимость намеренная (spec 0021 П-1): заявка ценнее языковой метки — кривой
# код языка не должен уронить создание заявки (ту же ошибку не прощает только
# строгая граница домена, куда уже уходит нормализованное значение).
_LANGUAGE_CODE = re.compile(r"^([a-z]{2})([-_][a-z0-9]+)*$")


class CreateServiceRequestArgs(BaseModel):
    """Аргументы, которые модель передаёт инструменту."""

    category_key: str
    summary: str = Field(min_length=1, max_length=500)
    room_number: str | None = Field(default=None, max_length=20)
    details: str | None = Field(default=None, max_length=4000)
    # UX-поле гейта P-9, НЕ персистится: вопрос-подтверждение гостю на его языке.
    # Модель почти всегда зовёт инструмент без свободного текста (замер: Sonnet и
    # Haiku на 6 языках дают tool_use с пустым text), поэтому естественный вопрос
    # берём из аргумента, а не из ответа модели. Оркестратор читает его для реплики
    # AWAITING_CONFIRMATION; сервис ядра его игнорирует. Optional — оборонительно
    # (старый pending_action без поля переживёт).
    confirmation_question: str | None = Field(default=None, max_length=500)
    # ISO 639-1 код языка последнего сообщения гостя (spec 0021 П-1): на нём
    # уйдут статусные уведомления о заявке. Optional и терпимый (см. валидатор):
    # старый pending_action без поля и кривое значение модели не валят заявку.
    guest_language: str | None = Field(default=None, max_length=35)

    @field_validator("guest_language", mode="before")
    @classmethod
    def _normalize_guest_language(cls, value: object) -> str | None:
        """«KK», «kk-KZ» → «kk»; не похожее на код языка → None (не ошибка)."""
        if not isinstance(value, str):
            return None
        match = _LANGUAGE_CODE.match(value.strip().lower())
        return match.group(1) if match else None


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
                "guest_language": {
                    "type": "string",
                    "description": (
                        "Код языка ПОСЛЕДНЕГО сообщения гостя по ISO 639-1, строчными "
                        "(например 'kk', 'ru', 'en', 'zh'). Определи по самому "
                        "сообщению, не по истории. На этом языке гость получит "
                        "уведомления о судьбе заявки."
                    ),
                },
                "confirmation_question": {
                    "type": "string",
                    "description": (
                        "Одна короткая, вежливая уточняющая фраза-вопрос гостю на ЕГО "
                        "языке (совпадает с языком последнего сообщения гостя), "
                        "спрашивающая, оформить ли эту заявку службе отеля. Обязательно "
                        "НАЗОВИ в вопросе, что именно будет сделано, и номер комнаты (если "
                        "известен), чтобы гость подтвердил именно нужную заявку, а не "
                        "угадывал. Должна звучать полностью естественно для носителя языка "
                        "(не дословный перевод) и быть вопросом о будущем действии — "
                        "никогда не утверждением, что уже сделано."
                    ),
                },
            },
            "required": ["category_key", "summary", "confirmation_question", "guest_language"],
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

    if args.guest_language is None and arguments.get("guest_language") is not None:
        # Модель прислала не-код («kazakh»?) — заявка важнее метки: создаём без языка,
        # уведомления уйдут на default_language тенанта (spec 0021 П-1, деградация).
        logger.warning("guest_language_not_normalized", raw=str(arguments.get("guest_language")))
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            summary=args.summary,
            room_number=args.room_number,
            details=args.details,
            guest_language=args.guest_language,
        )
    )

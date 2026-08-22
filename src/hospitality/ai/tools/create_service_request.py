"""CANONICAL инструмент AI: создать заявку службе отеля (Task 0015, P-5, §7.3).

Эталон паттерна «инструмент = тонкая обёртка над сервисом ядра». Копируется
всеми будущими инструментами. Логики нет — только контракт и обёртка над
`modules/requests`:

- вход для модели — `category_key` (slug), а НЕ `category_id` (UUID): модель не
  знает и не должна выдумывать внутренний UUID (§7.4, анти-галлюцинация).
  Оркестратор кладёт в схему `enum` реальных ключей тенанта; обёртка резолвит
  key→id тенантной сессией;
- класс подтверждения — `confirm_guest` (P-9): заявку создаёт гость, гейт
  исполнения — на оркестраторе. У СРОЧНОЙ заявки (`is_urgent`) гейт снят —
  сообщение «в номере течёт» и есть подтверждение (ADR-018, spec 0034 §5);
- `confirmation_question` — вопрос-подтверждение гостю на его языке (обязательный
  аргумент): модель почти всегда зовёт инструмент без свободного текста (замер:
  Sonnet и Haiku на 6 языках дают tool_use с пустым `text`), поэтому естественный
  вопрос берём из аргумента. UX-поле гейта: оркестратор показывает его гостю,
  сервис ядра игнорирует (не персистится).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationError, field_validator

from hospitality.ai import urgency
from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass, ToolTurnContext
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

NAME = "create_service_request"
CONFIRMATION_CLASS = ConfirmationClass.CONFIRM_GUEST
# Создание рождает заявку: по результату канал записывает привязку
# request_origins (ADR-011), а оркестратор заполняет created_request_id хода.
CREATES_REQUEST: Final = True
# Резерв «исполнено», если модель не дала текста (в норме даёт, на языке гостя).
DONE_TEXT: Final = "Готово, передаю в службу отеля."

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

_CATEGORY_DESCRIPTION = "Служба отеля — строго одно из допустимых значений."

# Шапка списка подсказок (issue #123). Один и тот же предмет намеренно стоит у
# двух служб («кофе в пакетиках» у housekeeping, «сваренный кофе» у fnb),
# поэтому здесь же — прямой запрет угадывать: канон промпта «лучше уточнить,
# чем угадать» ничем не привязан к предметам конкретного отеля, а эта строка
# привязана.
_CATEGORY_HINTS_HEADER = (
    "Что относится к каждой службе В ЭТОМ отеле (типовые примеры, список не исчерпывающий):"
)
_CATEGORY_HINTS_FOOTER = (
    "Если просьба гостя подходит сразу двум службам — НЕ угадывай и НЕ вызывай "
    "инструмент: сначала задай гостю короткий уточняющий вопрос, который "
    "разводит службы, и оформляй заявку уже по ответу."
)

# Язык гостя из аргумента модели: «kk», «KK», «kk-KZ» → «kk»; мусор → None.
# Терпимость намеренная (spec 0021 П-1): заявка ценнее языковой метки — кривой
# код языка не должен уронить создание заявки (ту же ошибку не прощает только
# строгая граница домена, куда уже уходит нормализованное значение).
_LANGUAGE_CODE = re.compile(r"^([a-z]{2})([-_][a-z0-9]+)*$")


def normalize_language_code(value: object) -> str | None:
    """«KK», «kk-KZ» → «kk»; не похожее на код языка → None (не ошибка).

    Одно правило для схемы аргументов и для реплики срочной заявки (`done_text`
    берёт язык из тех же сырых аргументов, ещё не прошедших схему).
    """
    if not isinstance(value, str):
        return None
    match = _LANGUAGE_CODE.match(value.strip().lower())
    return match.group(1) if match else None


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
    # Срочность заявки (spec 0034 §5): снимает гейт подтверждения (ADR-018) и
    # маркирует заявку персоналу. Optional с умолчанием False — безопасная
    # сторона: молчание модели = обычная заявка, гость подтверждает как прежде.
    is_urgent: bool = False

    @field_validator("guest_language", mode="before")
    @classmethod
    def _normalize_guest_language(cls, value: object) -> str | None:
        return normalize_language_code(value)

    @field_validator("is_urgent", mode="before")
    @classmethod
    def _normalize_is_urgent(cls, value: object) -> bool:
        """То же правило, что у `is_urgent(arguments)` — одно на оба пути (P-12).

        Иначе гейт (читает сырые аргументы до валидации) и запись в БД (читает
        схему) разошлись бы на кривом значении вроде строки «false».
        """
        return value is True


def is_urgent(arguments: Mapping[str, Any]) -> bool:
    """Помечен ли ЭТОТ вызов срочным (spec 0034 §5) — единственное правило (P-12).

    Строгое `is True`, а не `bool(...)`: строка «false» от модели — не «да».
    Безопасная сторона здесь — «обычная заявка»: она переспрашивает гостя, то
    есть даёт сегодняшнее поведение. Тем же правилом живёт и поле схемы
    (валидатор `_normalize_is_urgent` выше): гейт читает сырые аргументы, запись
    в БД — прошедшую схему, и разойтись на кривом значении они не вправе.
    """
    return arguments.get("is_urgent") is True


def confirmation_waived(arguments: Mapping[str, Any]) -> bool:
    """Снят ли гейт P-9 на этом вызове (контракт модуля инструмента, ADR-018).

    Срочная заявка исполняется сразу: гость, у которого в номере течёт, второго
    сообщения «да» не пишет — и заявки не появляется вовсе (аудит А-3).
    """
    return is_urgent(arguments)


def done_text(arguments: Mapping[str, Any]) -> str:
    """Резервная реплика «исполнено» под аргументы вызова (контракт модуля).

    На снятом гейте она не резервная, а единственная: свободный текст модели на
    таком ходу — вопрос-подтверждение (этого требует промпт), и показать его о
    уже созданной заявке значило бы соврать. Поэтому у срочной заявки —
    утверждённый абзац на языке гостя (spec 0034 §3), у обычной — прежний
    русский резерв.
    """
    if is_urgent(arguments):
        return urgency.urgent_accepted_reply(
            normalize_language_code(arguments.get("guest_language"))
        )
    return DONE_TEXT


def _category_description(category_keys: list[str], category_hints: dict[str, str] | None) -> str:
    """Описание поля `category_key`: список служб с подсказками тенанта."""
    hints = category_hints or {}
    lines = [f"- {key}: {hints[key]}" for key in category_keys if hints.get(key)]
    if not lines:
        return _CATEGORY_DESCRIPTION
    return "\n".join(
        [_CATEGORY_DESCRIPTION, "", _CATEGORY_HINTS_HEADER, *lines, "", _CATEGORY_HINTS_FOOTER]
    )


def build_spec(category_keys: list[str], category_hints: dict[str, str] | None = None) -> ToolSpec:
    """Собрать `ToolSpec` под текущий набор категорий тенанта (§7.4).

    `category_hints` (`TenantConfig.category_hints`, issue #123) — типовые
    предметы службы; попадают в описание enum'а. Подсказок нет — описание в
    точности прежнее: отель, который их не задал, не платит за них токенами.
    """
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "category_key": {
                    "type": "string",
                    "enum": category_keys,
                    "description": _category_description(category_keys, category_hints),
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
                "is_urgent": {
                    "type": "boolean",
                    "description": (
                        "true — ТОЛЬКО если промедление грозит здоровью, безопасности "
                        "или имуществу и заявку нельзя отложить даже на несколько минут: "
                        "течь или затопление, запах газа, нет отопления в мороз, "
                        "застрявший лифт, разбитое стекло, сломанный замок в номере. "
                        "Такая заявка уходит службе СРАЗУ, без вопроса гостю — поэтому "
                        "сомневаешься, ставь false. Обычное неудобство (полотенца, "
                        "уборка, еда, шумные соседи) — всегда false."
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


async def execute(
    arguments: dict[str, Any], context: ToolTurnContext
) -> requests_api.ServiceRequestRead:
    """Создать заявку из аргументов модели (внутри `tenant_context`, P-4).

    Категории читаются тенантной сессией — key резолвится в id только среди
    категорий этого тенанта (RLS, P-4). Ключ вне списка или невалидные
    аргументы — ERR-AI-004 (модель нарушила контракт).

    `context.verified_room_number` (spec 0027 §3.2) при наличии ПЕРЕЗАПИСЫВАЕТ
    комнату из аргументов модели: комната верифицированного гостя приходит из
    привязки Stay, а не из текста — «сделайте в 505-й» не переадресует заявку.
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
    room_number = args.room_number
    if context.verified_room_number is not None:
        if room_number is not None and room_number != context.verified_room_number:
            # Гость назвал другую комнату текстом — заявка всё равно уходит в его
            # верифицированную (spec 0027 §3.2); след для диагностики споров.
            logger.info(
                "room_number_overridden_by_binding",
                model_room=room_number,
                verified_room=context.verified_room_number,
            )
        room_number = context.verified_room_number
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            # Источник заявки (spec 0035 §4): этот инструмент — единственный
            # путь гостя, поэтому значение здесь константа, а не аргумент
            # модели. Из доли `guest_chat` считается Exit-критерий Phase 1 —
            # решать её моделью нельзя.
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary=args.summary,
            room_number=room_number,
            details=args.details,
            guest_language=args.guest_language,
            is_urgent=args.is_urgent,
        )
    )

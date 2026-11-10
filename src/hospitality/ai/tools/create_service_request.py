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
  сервис ядра игнорирует (не персистится);
- описания схемы — по-английски, как у остальных инструментов гостя: язык
  аргумента тянется за языком описания (#342). С русскими описаниями
  англичанин получал «Оформить заявку на доставку полотенец в номер 305?» —
  первые слова прежнего описания инструмента — примерно раз из четырёх.
  Подсказки служб (`category_hints`) пишет отель на своём языке — они не
  переводятся.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Self

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from hospitality.ai import urgency
from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools import service_quota
from hospitality.ai.tools.base import ConfirmationClass, PreparedCall, ToolTurnContext
from hospitality.modules.requests import api as requests_api
from hospitality.platform.service_quotas import MAX_QUOTA_UNITS, ServiceQuota
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

NAME = "create_service_request"
CONFIRMATION_CLASS = ConfirmationClass.CONFIRM_GUEST
# Создание рождает заявку: по результату канал записывает привязку
# request_origins (ADR-011), а оркестратор заполняет created_request_id хода.
CREATES_REQUEST: Final = True
# Резерв «исполнено», если модель не дала текста (в норме даёт, на языке гостя).
# Язык резерва неизвестен — kk, ru, en с новой строки, как статические тексты
# каналов (#224).
DONE_TEXT: Final = (
    "Дайын, өтінімді қонақүй қызметіне жіберемін.\n"
    "Готово, передаю в службу отеля.\n"
    "Done — I'm passing it to the hotel team."
)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): вызов инструмента моделью
# не соответствует контракту — неизвестный `category_key` (вне enum) или
# невалидные аргументы. Оркестратор превращает это в эскалацию, не в 5xx гостю.
ERR_AI_INVALID_TOOL_CALL = "ERR-AI-004"

_DESCRIPTION = (
    "Place a request with one of the hotel's services (housekeeping, maintenance, "
    "room service, IT and so on). Call it when the guest asks for something to be "
    "done in their room or for them. Take category_key ONLY from the allowed values "
    "(the enum); if none fits, do not call this tool — offer to bring in a staff "
    "member instead. summary is a short gist of the request in the guest's language."
)

_CATEGORY_DESCRIPTION = "The hotel service — strictly one of the allowed values."

# Шапка списка подсказок (issue #123). Один и тот же предмет намеренно стоит у
# двух служб («кофе в пакетиках» у housekeeping, «сваренный кофе» у fnb),
# поэтому здесь же — прямой запрет угадывать: канон промпта «лучше уточнить,
# чем угадать» ничем не привязан к предметам конкретного отеля, а эта строка
# привязана.
_CATEGORY_HINTS_HEADER = (
    "What belongs to each service IN THIS hotel (typical examples, not a complete list):"
)
_CATEGORY_HINTS_FOOTER = (
    "If the guest's request fits two services at once, do NOT guess and do NOT call "
    "the tool: first ask the guest, in the language of their last message, a short "
    "clarifying question that tells the services apart, and place the request once "
    "they answer."
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
    # Квота бесплатного (spec 0056 §4): пишет `prepare` — службу правила и
    # штуки по остатку; модельные значения до `execute` не доходят.
    quota_key: str | None = Field(default=None, max_length=63)
    quota_units: int | None = Field(default=None, ge=1, le=MAX_QUOTA_UNITS)

    @model_validator(mode="after")
    def _quota_pair(self) -> Self:
        """Ключ и штуки — только парой: так их держит CHECK `service_requests`."""
        if (self.quota_key is None) != (self.quota_units is None):
            raise ValueError("quota_key and quota_units go together")
        return self

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


async def prepare(arguments: dict[str, Any], context: ToolTurnContext) -> PreparedCall:
    """Вызов модели → вызов для гейта P-9 (контракт модуля, spec 0056 §4).

    Срочная заявка квоту не проходит (Р-5): гейт снят, проверять до «да»
    нечего, а метка квоты модели из неё выбрасывается — в счёт идёт только то,
    что проверил код. Остальное — `service_quota.prepare_quota`.
    """
    if is_urgent(arguments):
        return PreparedCall(
            arguments={
                key: value
                for key, value in arguments.items()
                if key not in (service_quota.QUOTA_KEY_ARGUMENT, service_quota.QUOTA_UNITS_ARGUMENT)
            }
        )
    return await service_quota.prepare_quota(
        arguments, context, language=normalize_language_code(arguments.get("guest_language"))
    )


def done_text(arguments: Mapping[str, Any]) -> str:
    """Резервная реплика «исполнено» под аргументы вызова (контракт модуля).

    На снятом гейте она не резервная, а единственная: свободный текст модели на
    таком ходу — вопрос-подтверждение (этого требует промпт), и показать его о
    уже созданной заявке значило бы соврать. Поэтому у срочной заявки —
    утверждённый абзац на языке гостя (spec 0034 §3), у платной сверх квоты —
    цена из конфига (spec 0056 §5), у обычной — трёхъязычный резерв `DONE_TEXT`.
    """
    language = normalize_language_code(arguments.get("guest_language"))
    if is_urgent(arguments):
        return urgency.urgent_accepted_reply(language)
    offer = service_quota.offer_from_arguments(arguments)
    if offer is not None:
        return service_quota.paid_done_text(offer, language)
    return DONE_TEXT


def done_text_is_final(arguments: Mapping[str, Any]) -> bool:
    """Отдать гостю `done_text`, а не реплику классификатора (контракт модуля).

    Истина у платной заявки сверх квоты (spec 0056 Р-7): классификатор мог бы
    пересказать цену иначе, а цену гостю называет только код.
    """
    return service_quota.offer_from_arguments(arguments) is not None


def _category_description(category_keys: list[str], category_hints: dict[str, str] | None) -> str:
    """Описание поля `category_key`: список служб с подсказками тенанта."""
    hints = category_hints or {}
    lines = [f"- {key}: {hints[key]}" for key in category_keys if hints.get(key)]
    if not lines:
        return _CATEGORY_DESCRIPTION
    return "\n".join(
        [_CATEGORY_DESCRIPTION, "", _CATEGORY_HINTS_HEADER, *lines, "", _CATEGORY_HINTS_FOOTER]
    )


def _quota_properties(service_quotas: Sequence[ServiceQuota]) -> dict[str, Any]:
    """Аргументы квоты бесплатного (spec 0056 §4): enum ключей отеля и штуки."""
    listing = "\n".join(f"- {quota.key}: {quota.names.en}" for quota in service_quotas)
    return {
        service_quota.QUOTA_KEY_ARGUMENT: {
            "type": "string",
            "enum": [quota.key for quota in service_quotas],
            "description": (
                "The hotel gives these items free of charge up to a set allowance. If "
                "the guest asks for an item from this list, pass its key — the system "
                "works out how many are free and what goes beyond the allowance. For a "
                "request about anything else, leave this field out.\n" + listing
            ),
        },
        service_quota.QUOTA_UNITS_ARGUMENT: {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_QUOTA_UNITS,
            "description": (
                "How many items the guest asks for; if no number is named — 1. Pass it "
                "together with quota_key."
            ),
        },
    }


def build_spec(
    category_keys: list[str],
    category_hints: dict[str, str] | None = None,
    service_quotas: Sequence[ServiceQuota] = (),
) -> ToolSpec:
    """Собрать `ToolSpec` под текущий набор категорий тенанта (§7.4).

    `category_hints` (`TenantConfig.category_hints`, issue #123) — типовые
    предметы службы; попадают в описание enum'а. Подсказок нет — описание в
    точности прежнее: отель, который их не задал, не платит за них токенами.
    `service_quotas` — то же правило (spec 0056 §4): аргументы квоты есть, только
    если у отеля есть квоты, иначе схема байт в байт прежняя.
    """
    spec = ToolSpec(
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
                    "description": "A short gist of the request, in the guest's language.",
                },
                "room_number": {
                    "type": "string",
                    "description": "The room number, if known.",
                },
                "details": {
                    "type": "string",
                    "description": "Additional details, if any.",
                },
                "guest_language": {
                    "type": "string",
                    "description": (
                        "The ISO 639-1 code of the language of the guest's LAST message, "
                        "in lower case (for example 'kk', 'ru', 'en', 'zh'). Work it out "
                        "from that message itself, not from the history. The guest gets "
                        "the updates about this request in this language."
                    ),
                },
                "is_urgent": {
                    "type": "boolean",
                    "description": (
                        "true ONLY if a delay threatens health, safety or property and "
                        "the request cannot wait even a few minutes: a leak or flooding, "
                        "a smell of gas, no heating in freezing weather, a stuck lift, "
                        "broken glass, a broken lock on the room. Such a request goes to "
                        "the service AT ONCE, without asking the guest — so if in doubt, "
                        "set false. An ordinary inconvenience (towels, cleaning, food, "
                        "noisy neighbours) is always false."
                    ),
                },
                "confirmation_question": {
                    "type": "string",
                    "description": (
                        "One short, polite question to the guest, in the language of the "
                        "guest's last message — the language you put in guest_language — "
                        "asking whether to place exactly this request with the hotel "
                        "service. Always NAME in the question what will be done, and the "
                        "room number if known, so that the guest confirms the right "
                        "request instead of guessing. It must sound fully natural to a "
                        "native speaker (not a word-for-word translation) and be a "
                        "question about a future action — never a statement that it is "
                        "already done."
                    ),
                },
            },
            "required": ["category_key", "summary", "confirmation_question", "guest_language"],
        },
    )
    if service_quotas:
        spec.input_schema["properties"].update(_quota_properties(service_quotas))
    return spec


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
    `context.verified_stay_id` — тот же Stay — уходит в заявку (spec 0054).
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
        ),
        stay_id=context.verified_stay_id,
        quota_key=args.quota_key,
        quota_units=args.quota_units,
    )

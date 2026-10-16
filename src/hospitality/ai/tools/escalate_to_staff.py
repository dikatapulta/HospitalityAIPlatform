"""AI-инструмент: позвать сотрудника к гостю (spec 0040, issue #101, класс `auto`).

До него модель обещала человека свободным текстом («уточню у коллег»), ход
приходил `REPLY`, и персонал о госте не узнавал — третий путь обещания без
эффекта после двух, закрытых spec 0022. Теперь обещание — вызов инструмента,
и его эффект — существующая эскалация spec 0022 целиком: исход `NEEDS_HUMAN`
с `EscalationContext`, по которому канал заводит карточку в кабинете (spec
0039 §2) и публикует `conversation.escalated`.

Отличие от канона `create_service_request`: исполнения в ai/ нет, поэтому
модуля нет в `registry._TOOLS` (контракт реестра — `execute` возвращает
заявку). Позвать человека — забота канала, а AI-слой каналы не импортирует;
оркестратор перехватывает вызов ДО диспетчера реестра и отдаёт каналу
контекст. Схему собирает реестр — там же, где решается состав инструментов
хода и действует флаг заявок.

Описания полей — по-английски, как у сигнала spec 0036 §6: `reply_to_guest`
гость читает сам, а язык аргумента тянется за языком его описания (#342).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway.api import ToolSpec

NAME = "escalate_to_staff"

# Код каталога (docs/runbooks/errors.md): не сбой, а событие «модель позвала
# сотрудника» — тот же класс, что ERR-CHANNEL-005 у ЧП. Нужен контексту
# эскалации (поле обязательно) и журналу.
ERR_AI_STAFF_REQUESTED: Final = "ERR-AI-005"

# Последний рубеж реплики: модель не дала ни аргумента, ни свободного текста.
# Двуязычный, как DEGRADED_REPLY канала: язык гостя здесь неизвестен.
FALLBACK_REPLY: Final = (
    "Передал вашу просьбу сотрудникам отеля — к вам подойдут или позвонят в номер. "
    "I've passed your request to the hotel staff — someone will come by or call your room."
)

_DESCRIPTION = (
    "Call a hotel staff member to this guest. Hotel staff are notified at once, and "
    "someone comes by or calls the guest's room. This is the only way to reach staff: "
    "call it whenever the guest asks for a person or says yes to your offer of one, "
    "and on every turn on which you tell the guest that someone from the staff will "
    "help — words without this call reach nobody. Merely offering a staff member is "
    "not a reason to call it."
)

_SUMMARY_DESCRIPTION = (
    "What the guest needs from a staff member, in one short sentence, in the guest's "
    "language. Staff read it next to the guest's last message, which may be just 'yes'."
)

_REPLY_DESCRIPTION = (
    "Your whole reply to the guest, in the language of the guest's last message: say "
    "that you have passed it to the hotel staff and that someone will come by or call "
    "their room. Staff cannot answer in this chat, and you do not know how soon they "
    "will come — promise neither."
)

# Пределы — те же, что у суть/реплики соседних инструментов: длиннее модель не
# пишет, а суть всё равно обрезается шаблоном карточки (spec 0039 §2.2).
_SUMMARY_MAX_CHARS: Final = 500
_REPLY_MAX_CHARS: Final = 1000


def build_spec() -> ToolSpec:
    """Схема инструмента (§7.4). Входа от тенанта нет: служба эскалаций — у канала."""
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "maxLength": _SUMMARY_MAX_CHARS,
                    "description": _SUMMARY_DESCRIPTION,
                },
                "reply_to_guest": {
                    "type": "string",
                    "maxLength": _REPLY_MAX_CHARS,
                    "description": _REPLY_DESCRIPTION,
                },
            },
            "required": ["summary", "reply_to_guest"],
        },
    )


def escalation_context(
    arguments: Mapping[str, Any], *, verified_room_number: str | None
) -> EscalationContext:
    """Контекст эскалации из вызова. Аргументы не валидируются строго намеренно:
    кривой вызов не повод не звать человека — гость уже его ждёт."""
    summary = _text(arguments, "summary", _SUMMARY_MAX_CHARS)
    return EscalationContext(
        reason=EscalationReason.GUEST_REQUESTED_STAFF,
        error_code=ERR_AI_STAFF_REQUESTED,
        tool_name=NAME,
        action_summary=summary or None,
        room_number=verified_room_number,
    )


def reply_text(arguments: Mapping[str, Any], model_text: str) -> str:
    """Реплика гостю: аргумент → свободный текст модели → двуязычный резерв."""
    return _text(arguments, "reply_to_guest", _REPLY_MAX_CHARS) or (
        model_text.strip() or FALLBACK_REPLY
    )


def _text(arguments: Mapping[str, Any], key: str, max_chars: int) -> str:
    value = arguments.get(key)
    return value.strip()[:max_chars] if isinstance(value, str) else ""

"""AI-инструмент: гость напоминает о своей открытой заявке (spec 0070 §4, issue #834, класс `auto`).

До него промпт велел отвечать на «где мои полотенца?» статусом из снапшота без
вызова, и то, что гость ждёт и недоволен, не узнавал никто. Теперь каждый такой
вопрос — вызов, а его эффект — счёт напоминаний на самой заявке
(`requests_api.record_guest_reminder`). Кого звать, решает лестница кабинета по
настройке отеля (spec 0070 §5), а не модель (P-5).

Отличие от канона `create_service_request` — напоминание сопутствующий вызов,
как сигнал «факта нет» (spec 0036 §6): оркестратор вынимает его вызовы из хода
до выбора действия (`take_reminders`), независимо от позиции, и у действия они
не выигрывают — «где полотенца? и принесите воды» не теряет заявку на воду.
Поэтому модуля нет в `registry._TOOLS`: диспетчер реестра его не видит, как и
`escalate_to_staff`. Схему собирает реестр — только при непустом снапшоте, и
флаг заявок её не выключает: заявки, созданные до выключения, гость вправе
торопить (как отмену). Класс `auto` (P-9): не деньги, не документы, не бронь —
эффект один, звук в кабинете.

Описания — по-английски: `reply_to_guest` гость читает сам, а язык аргумента
тянется за языком его описания (#342).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from hospitality.ai.gateway.api import ToolCall, ToolSpec
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass, ToolTurnContext
from hospitality.modules.requests import api as requests_api
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

NAME = "remind_about_request"
# Свойство контракта (P-9), а не ветка гейта: до гейта вызов не доходит —
# оркестратор вынимает его раньше выбора действия.
CONFIRMATION_CLASS: Final = ConfirmationClass.AUTO

# Последний рубеж реплики (spec 0070 §7): модель не дала ни аргумента, ни
# свободного текста. Язык гостя неизвестен — kk, ru, en с новой строки (#224).
FALLBACK_REPLY: Final = (
    "Еске салуыңызды өтінімге белгіледім — қызметкерлер оны көреді.\n"
    "Отметил ваше напоминание в заявке — сотрудники его видят.\n"
    "I've added your reminder to the request — the staff can see it."
)

_DESCRIPTION = (
    "Record that the guest is asking about, or reminding you of, one of their open "
    "requests from the Active service requests list. Call it on every message where "
    "the guest asks how such a request is going or says it is still not done. Pick "
    "request_id ONLY from the allowed values. It does not create, cancel or speed up "
    "anything by itself. If on the same message the guest asks for a person (a "
    "manager, the reception, a staff member), do not call this tool: that is a request "
    "for a staff member, not a reminder."
)

_REQUEST_ID_DESCRIPTION = (
    "The id of the request the guest is asking about — strictly one of the allowed "
    "values (see the Active service requests block)."
)

_REPLY_DESCRIPTION = (
    "Your whole reply to the guest, in the language of the guest's last message: that "
    "you have noted the reminder, the request's #N and its status in plain words. "
    "Promise no time, and never say that a manager or anyone else was alerted."
)

# Предел — как у реплики `escalate_to_staff`: длиннее модель не пишет.
_REPLY_MAX_CHARS: Final = 1000


@dataclass(frozen=True)
class Reminders:
    """Вызовы напоминания одного хода — уже записанные (spec 0070 §4).

    `reply_to_guest` — реплика только из АРГУМЕНТОВ и может быть пустой:
    запасную ступень выбирает `reply_text` по исходу хода, как у сигнала
    «факта нет» (`orchestrator._signal_reply`).
    """

    reply_to_guest: str


def build_spec(active_requests: tuple[ActiveRequest, ...]) -> ToolSpec:
    """Схема под открытые заявки диалога (§7.4). Вызывается при непустом списке."""
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "enum": [str(request.id) for request in active_requests],
                    "description": _REQUEST_ID_DESCRIPTION,
                },
                "reply_to_guest": {
                    "type": "string",
                    "maxLength": _REPLY_MAX_CHARS,
                    "description": _REPLY_DESCRIPTION,
                },
            },
            "required": ["request_id", "reply_to_guest"],
        },
    )


async def take_reminders(
    tool_calls: Sequence[ToolCall], context: ToolTurnContext
) -> tuple[Reminders | None, list[ToolCall]]:
    """Вынуть напоминания из вызовов хода и записать их (внутри `tenant_context`).

    Возвращает разобранные напоминания (None — их не было) и ОСТАВШИЕСЯ вызовы:
    с ними ход работает ровно как раньше. Каждый различный `request_id` — одна
    запись, сколько бы раз модель его ни назвала: «где? где?» одним сообщением —
    одно напоминание. id вне снапшота хода и заявка, закрытая между ходами, —
    пропуск с warning, без эскалации: гость спрашивал о статусе, а не просил
    действия.
    """
    calls = [call for call in tool_calls if call.name == NAME]
    remaining = [call for call in tool_calls if call.name != NAME]
    if not calls:
        return None, remaining
    for request_id in _distinct_values(calls, "request_id"):
        await _record(request_id, context)
    replies = _distinct_values(calls, "reply_to_guest")
    return Reminders(reply_to_guest="\n\n".join(replies)), remaining


def reply_text(reminders: Reminders, model_text: str) -> str:
    """Реплика гостю: аргумент → свободный текст модели → трёхъязычный резерв."""
    return reminders.reply_to_guest or model_text.strip() or FALLBACK_REPLY


async def _record(raw_id: str, context: ToolTurnContext) -> None:
    try:
        request_id = uuid.UUID(raw_id)
    except ValueError:
        logger.warning("guest_reminder_skipped", reason="not_in_snapshot")
        return
    if context.find_active_request(request_id) is None:
        logger.warning(
            "guest_reminder_skipped", reason="not_in_snapshot", request_id=str(request_id)
        )
        return
    # `guest_reminder_recorded` с числом напоминаний пишет сам домен.
    if await requests_api.record_guest_reminder(request_id) is None:
        logger.warning("guest_reminder_skipped", reason="closed", request_id=str(request_id))


def _distinct_values(calls: Sequence[ToolCall], key: str) -> list[str]:
    """Непустые строковые значения аргумента по всем вызовам, без повторов, по порядку."""
    values = (_text(call.arguments, key) for call in calls)
    return list(dict.fromkeys(value for value in values if value))


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value.strip()[:_REPLY_MAX_CHARS] if isinstance(value, str) else ""

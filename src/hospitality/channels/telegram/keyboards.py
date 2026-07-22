"""Inline-клавиатуры заявки в staff-чате (spec 0021 П-2, issue #38 п.2).

Кнопки — тот же переход `STATUS_TRANSITIONS`, что и команды (P-5, DoD #38):
`callback_data` кодирует заявку и действие (`req:<uuid>:<действие>`), нажатие
исполняет `requests.change_request_status`. Клавиатура зависит только от
статуса заявки — после перехода бот перерисовывает её под новый статус,
устаревшие кнопки безопасны (их гасит карта переходов, персонал видит тост).

Формат словарей — Telegram Bot API `InlineKeyboardMarkup`; типизированной
обёртки нет намеренно: это единственное место, где он собирается.
"""

from __future__ import annotations

import enum
import uuid

from hospitality.modules.requests import api as requests_api

# Разделитель и префикс callback_data: "req:<uuid>:<действие>" = 46 байт —
# в лимит Telegram (64) помещается с запасом.
_CALLBACK_PREFIX = "req"
_SEPARATOR = ":"


class CallbackAction(enum.StrEnum):
    """Действие кнопки. START/DONE/CANCEL — прямые переходы жизненного цикла;
    DONE_NOTE — «готово частично»: бот сначала спрашивает примечание (ForceReply),
    переход происходит после ответа персонала (spec 0021 П-4)."""

    START = "start"
    DONE = "done"
    DONE_NOTE = "done_note"
    CANCEL = "cancel"


# Действие → целевой статус (DONE_NOTE не здесь: он двухшаговый).
STATUS_BY_ACTION: dict[CallbackAction, requests_api.RequestStatus] = {
    CallbackAction.START: requests_api.RequestStatus.IN_PROGRESS,
    CallbackAction.DONE: requests_api.RequestStatus.DONE,
    CallbackAction.CANCEL: requests_api.RequestStatus.CANCELLED,
}


def build_callback_data(request_id: uuid.UUID, action: CallbackAction) -> str:
    return _SEPARATOR.join((_CALLBACK_PREFIX, str(request_id), action.value))


def parse_callback_data(data: str) -> tuple[uuid.UUID, CallbackAction] | None:
    """Разобрать callback_data кнопки; None — не наш формат (чужая/старая кнопка)."""
    parts = data.split(_SEPARATOR)
    if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX:
        return None
    try:
        return uuid.UUID(parts[1]), CallbackAction(parts[2])
    except ValueError:
        return None


def keyboard_for_status(
    request_id: uuid.UUID, status: requests_api.RequestStatus
) -> dict[str, object] | None:
    """InlineKeyboardMarkup под текущий статус заявки; None — кнопок нет (терминал)."""

    def button(label: str, action: CallbackAction) -> dict[str, str]:
        return {"text": label, "callback_data": build_callback_data(request_id, action)}

    if status is requests_api.RequestStatus.NEW:
        rows = [
            [button("🏃 Взять в работу", CallbackAction.START)],
            [button("❌ Отменить", CallbackAction.CANCEL)],
        ]
    elif status is requests_api.RequestStatus.IN_PROGRESS:
        rows = [
            [button("✅ Готово", CallbackAction.DONE)],
            [
                button("⚠️ Готово частично", CallbackAction.DONE_NOTE),
                button("❌ Отменить", CallbackAction.CANCEL),
            ],
        ]
    else:
        return None
    return {"inline_keyboard": rows}

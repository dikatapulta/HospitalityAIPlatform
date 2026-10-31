"""Inline-клавиатуры заявки в staff-чате (spec 0021 П-2, issue #38 п.2).

Кнопки — тот же переход `STATUS_TRANSITIONS`, что и команды (P-5, DoD #38):
`callback_data` кодирует заявку и действие (`req:<uuid>:<действие>`), нажатие
исполняет `requests.change_request_status`. Клавиатура зависит только от
статуса заявки — после перехода бот перерисовывает её под новый статус,
устаревшие кнопки безопасны (их гасит карта переходов, персонал видит тост).

«❌ Отменить» перехода не делает: под тем же сообщением открывает список причин
(spec 0057 §2), и уже кнопка причины (`req:<uuid>:cr:<причина>`) отменяет заявку.

Формат словарей — Telegram Bot API `InlineKeyboardMarkup`; типизированной
обёртки нет намеренно: это единственное место, где он собирается.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from hospitality.modules.requests import api as requests_api

# Разделитель и префикс callback_data: "req:<uuid>:<действие>" = 46 байт, с причиной
# отмены "req:<uuid>:cr:guest_declined" — 58: в лимит Telegram (64) помещается.
# Длиннее 64 байт Bot API отвергает всю клавиатуру (тест держит каждую кнопку).
_CALLBACK_PREFIX = "req"
_SEPARATOR = ":"


class CallbackAction(enum.StrEnum):
    """Действие кнопки. START/DONE/CANCEL_REASON — переходы жизненного цикла
    (у CANCEL_REASON четвёртым сегментом — причина отмены, spec 0057 §2).
    DONE_NOTE — «готово частично»: бот сначала спрашивает примечание (ForceReply),
    переход происходит после ответа персонала (spec 0021 П-4). CANCEL открывает
    список причин, BACK возвращает кнопки статуса — оба без перехода.
    Значение CANCEL прежнее: кнопки «❌ Отменить» в уже отправленных уведомлениях
    тоже открывают список, а не отменяют без причины."""

    START = "start"
    DONE = "done"
    DONE_NOTE = "done_note"
    CANCEL = "cancel"
    CANCEL_REASON = "cr"
    BACK = "back"


# Действие → целевой статус (DONE_NOTE, CANCEL и BACK не здесь: перехода у них нет
# или он двухшаговый; CANCEL_REASON с причиной `other` — тоже, через вопрос).
STATUS_BY_ACTION: dict[CallbackAction, requests_api.RequestStatus] = {
    CallbackAction.START: requests_api.RequestStatus.IN_PROGRESS,
    CallbackAction.DONE: requests_api.RequestStatus.DONE,
    CallbackAction.CANCEL_REASON: requests_api.RequestStatus.CANCELLED,
}

# Причины, которые выбирает персонал: все, кроме `by_guest` — её ставит домен на
# отмене гостем (spec 0057 §2). Порядок кнопок — порядок `CancelReason`.
STAFF_CANCEL_REASONS: tuple[requests_api.CancelReason, ...] = tuple(
    reason
    for reason in requests_api.CancelReason
    if reason is not requests_api.CancelReason.BY_GUEST
)


@dataclass(frozen=True)
class CallbackData:
    """Разобранная кнопка: заявка, действие и — у CANCEL_REASON — причина отмены."""

    request_id: uuid.UUID
    action: CallbackAction
    reason: requests_api.CancelReason | None = None


def build_callback_data(
    request_id: uuid.UUID,
    action: CallbackAction,
    reason: requests_api.CancelReason | None = None,
) -> str:
    parts = [_CALLBACK_PREFIX, str(request_id), action.value]
    if reason is not None:
        parts.append(reason.value)
    return _SEPARATOR.join(parts)


def parse_callback_data(data: str) -> CallbackData | None:
    """Разобрать callback_data кнопки; None — не наш формат (чужая/старая кнопка).

    Причина — только у CANCEL_REASON и только из `STAFF_CANCEL_REASONS`: кнопку
    с `by_guest` бот не рисует, и подделанная так не отменит «от имени гостя».
    """
    parts = data.split(_SEPARATOR)
    if len(parts) not in (3, 4) or parts[0] != _CALLBACK_PREFIX:
        return None
    try:
        request_id, action = uuid.UUID(parts[1]), CallbackAction(parts[2])
        reason = requests_api.CancelReason(parts[3]) if len(parts) == 4 else None
    except ValueError:
        return None
    if (action is CallbackAction.CANCEL_REASON) != (reason is not None):
        return None
    if reason is not None and reason not in STAFF_CANCEL_REASONS:
        return None
    return CallbackData(request_id, action, reason)


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


def cancel_reasons_keyboard(request_id: uuid.UUID) -> dict[str, object]:
    """Список причин отмены вместо кнопок статуса (spec 0057 §2): по одной в ряд
    (подписи длинные) и «← Назад». Подписи — общий словарь кабинета и чата."""
    rows = [
        [
            {
                "text": requests_api.CANCEL_REASON_LABELS[reason],
                "callback_data": build_callback_data(
                    request_id, CallbackAction.CANCEL_REASON, reason
                ),
            }
        ]
        for reason in STAFF_CANCEL_REASONS
    ]
    rows.append(
        [{"text": "← Назад", "callback_data": build_callback_data(request_id, CallbackAction.BACK)}]
    )
    return {"inline_keyboard": rows}

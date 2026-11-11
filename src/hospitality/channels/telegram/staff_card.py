"""Карточка заявки в чате службы: одна заявка — одно сообщение (issue #92, spec 0021 П-2).

Уведомление о новой заявке и есть карточка. При смене статуса бот перерисовывает
её — текст и кнопки одним `editMessageText`, — а не пишет новую строку в группу:
правка не будит чат пушем, поэтому «в работе» проходит тихо. Отдельным сообщением
в группу уходит только итог закрытия (`staff.py`) и отмена гостем
(`notifications.py`).

Формат карточки живёт здесь целиком и в обе стороны: `render` собирает текст из
тела (категория, комната, суть — их собирает `notifications.py`, суть переводится
моделью один раз) и статуса заявки, `body_of` достаёт тело из текста, с которым
уведомление ушло. Перевод при смене статуса заново не зовётся: тело берётся из
исходящего `Message` уведомления.
"""

from __future__ import annotations

from hospitality.channels.common.store import StaffNotification, load_staff_notification_target
from hospitality.channels.telegram import keyboards, staff_cancel
from hospitality.channels.telegram.client import TelegramSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

_URGENT_MARK = "🚨 СРОЧНО · "

# Заголовок по статусу; `{label}` — « #N» или « от гостя» у заявки без номера.
_HEADERS: dict[requests_api.RequestStatus, str] = {
    requests_api.RequestStatus.NEW: "🔔 Новая заявка{label}",
    requests_api.RequestStatus.IN_PROGRESS: "🏃 Заявка{label} — в работе",
    requests_api.RequestStatus.DONE: "✅ Заявка{label} — выполнена",
    requests_api.RequestStatus.CANCELLED: "❌ Заявка{label} — отменена",
}
# `done` с примечанием персонала — «выполнено частично» (spec 0021 П-4).
_PARTIAL_HEADER = "⚠️ Заявка{label} — выполнена частично"


def render(body: str, request: requests_api.ServiceRequestRead) -> str:
    """Текст карточки: заголовок со статусом, тело, затем ход или итог.

    Для статуса `new` — ровно прежнее уведомление о новой заявке. Открытая заявка
    заканчивается строкой «Ход:» с командами под её статус, закрытая — причиной
    и примечанием персонала, если они есть.
    """
    if request.status in (requests_api.RequestStatus.NEW, requests_api.RequestStatus.IN_PROGRESS):
        return f"{_header(request)}\n{body}\n\n{_action_line(request)}"
    outcome = [line for line in (staff_cancel.reason_line(request), _note_line(request)) if line]
    text = f"{_header(request)}\n{body}"
    return f"{text}\n\n" + "\n".join(outcome) if outcome else text


def body_of(sent_text: str) -> str | None:
    """Тело из текста уведомления, как оно ушло (`render` при статусе `new`).

    Первая строка — заголовок, последний абзац — «Ход:». Тело между ними; суть
    гостя может нести пустые строки, поэтому абзац ищется с конца. None — текст
    не похож на карточку: тогда меняются только кнопки, как до #92.
    """
    _, has_header, rest = sent_text.partition("\n")
    body, has_action, _ = rest.rpartition("\n\n")
    if not has_header or not has_action or not body:
        return None
    return body


async def refresh(
    request: requests_api.ServiceRequestRead,
    sender: TelegramSender,
    *,
    pressed: tuple[str, str] | None = None,
) -> None:
    """Перерисовать карточку под текущий статус заявки (best-effort).

    `pressed` — (чат, сообщение) нажатой кнопки. Кнопки стоят не только под
    карточкой, но и под напоминаниями о невзятой и зависшей заявке (spec 0028):
    у такого сообщения меняются только кнопки, а карточка — своим чередом. Сбой
    Bot API перехода не отменяет: устаревшие кнопки гасит карта переходов.
    """
    markup = keyboards.keyboard_for_status(request.id, request.status)
    card = await load_staff_notification_target(request.id)
    if card is not None:
        await _edit_card(card, request, markup, sender)
    if pressed is not None and (card is None or pressed != (card.chat_id, card.message_id)):
        await _edit_keyboard(pressed[0], pressed[1], markup, sender)


async def _edit_card(
    card: StaffNotification,
    request: requests_api.ServiceRequestRead,
    markup: dict[str, object] | None,
    sender: TelegramSender,
) -> None:
    body = body_of(card.text) if card.text else None
    if body is None:
        await _edit_keyboard(card.chat_id, card.message_id, markup, sender)
        return
    try:
        await sender.edit_message_text(card.chat_id, card.message_id, render(body, request), markup)
    except Exception as error:  # best-effort: карточка — удобство, не инвариант
        # Текст не принят (длина, «не изменилось») — кнопки всё равно под статус.
        logger.info("staff_card_refresh_failed", error=str(error))
        await _edit_keyboard(card.chat_id, card.message_id, markup, sender)
        return
    logger.info("staff_card_refreshed", request_id=str(request.id), status=request.status.value)


async def _edit_keyboard(
    chat_id: str, message_id: str, markup: dict[str, object] | None, sender: TelegramSender
) -> None:
    try:
        await sender.edit_message_reply_markup(chat_id, message_id, markup)
    except Exception as error:  # best-effort: кнопки — удобство, не инвариант
        logger.info("staff_keyboard_refresh_failed", error=str(error))


def _header(request: requests_api.ServiceRequestRead) -> str:
    label = f" #{request.daily_number}" if request.daily_number is not None else " от гостя"
    partial = request.status is requests_api.RequestStatus.DONE and bool(request.resolution_note)
    template = _PARTIAL_HEADER if partial else _HEADERS[request.status]
    is_open = request.status in (
        requests_api.RequestStatus.NEW,
        requests_api.RequestStatus.IN_PROGRESS,
    )
    # Срочность (spec 0034 §5) кричит, пока заявка открыта; у закрытой — шум.
    mark = _URGENT_MARK if request.is_urgent and is_open else ""
    return mark + template.format(label=label)


def _action_line(request: requests_api.ServiceRequestRead) -> str:
    """Команды под статус: номер `#N` — аргумент; у заявки без номера — её id."""
    new = request.status is requests_api.RequestStatus.NEW
    number = request.daily_number
    if number is None:
        verbs = "/start · /done · /cancel" if new else "/done · /cancel"
        return f"id: {request.id}\nХод: {verbs} + этот id."
    if new:
        return f"Ход: /start {number} (взять в работу) · /done {number} · /cancel {number}"
    return f"Ход: /done {number} · /cancel {number}"


def _note_line(request: requests_api.ServiceRequestRead) -> str | None:
    return f"Примечание: {request.resolution_note}" if request.resolution_note else None

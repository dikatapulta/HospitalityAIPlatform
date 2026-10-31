"""Отмена заявки из staff-чата: причина списком (spec 0057 §2, PR 2 серии #54).

«❌ Отменить» перехода не делает: под нажатым сообщением открывает список причин
(`keyboards.cancel_reasons_keyboard`), «← Назад» возвращает кнопки статуса.
Причина из списка отменяет заявку сразу; «Другое — напишу сам» задаёт вопрос
с ForceReply (ключ `staff:cancel_prompt:<id>:…`), и ответ закрывает заявку
с `other` — его текст прочитает гость. `/cancel #N` без причины не отменяет,
а подсказывает: причина обязательна, как в кабинете.

Сам переход — общий `_apply_transition` в `staff.py` (P-5); здесь — то, чем
путь отмены отличается от прочих: тексты, смена кнопок, подсказка.
"""

from __future__ import annotations

from hospitality.channels.base import NormalizedMessage
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

CANCEL_QUESTION = (
    "Заявка {label}: почему отменяете? Гость прочитает ответ. Ответьте на это сообщение."
)
CANCEL_WAITING = "Жду причину отмены {label} ответом на вопрос."


async def swap_keyboard(
    request: requests_api.ServiceRequestRead,
    label: str,
    normalized: NormalizedMessage,
    sender: TelegramSender,
    *,
    reasons: bool,
) -> str:
    """Сменить кнопки под нажатым сообщением: список причин или снова статус.

    Возвращает текст тоста. Заявку уже закрыли (кабинетом, другим чатом) —
    кнопки снимаются: список причин к закрытой заявке только родил бы отказ
    перехода на следующем нажатии.
    """
    status_markup = keyboards.keyboard_for_status(request.id, request.status)
    markup: dict[str, object] | None
    if status_markup is None:
        markup, toast = None, f"Заявка {label} уже закрыта."
    elif reasons:
        markup = keyboards.cancel_reasons_keyboard(request.id)
        toast = f"Заявка {label}: выберите причину отмены."
    else:
        markup, toast = status_markup, f"Заявка {label} не отменена."
    if normalized.reply_to is not None:
        try:
            await sender.edit_message_reply_markup(
                normalized.chat_id, normalized.reply_to.external_message_id, markup
            )
        except Exception as error:  # best-effort: кнопки — удобство, не инвариант
            logger.info("staff_keyboard_refresh_failed", error=str(error))
    return toast


def reason_hint(request: requests_api.ServiceRequestRead, label: str) -> str:
    """Ответ на `/cancel #N` без причины: куда её вписать (spec 0057 §2)."""
    argument = request.daily_number if request.daily_number is not None else request.id
    return (
        f"Заявка {label}: выберите причину кнопкой «❌ Отменить» под уведомлением — "
        f"или напишите её словами: /cancel {argument} <причина>. Гость прочитает её."
    )


def reason_line(request: requests_api.ServiceRequestRead) -> str | None:
    """Строка итога отмены в чат: подпись причины из списка.

    У `other` строки нет — его текст и так стоит строкой «Примечание».
    """
    reason = request.cancel_reason
    if reason is None or reason is requests_api.CancelReason.OTHER:
        return None
    return f"Причина: {requests_api.CANCEL_REASON_LABELS[reason]}"

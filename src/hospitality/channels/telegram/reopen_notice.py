"""Поправка гостю: выполненную заявку вернули в работу (spec 0080 §4, issue #55).

Подписчик `request.status_changed` (`done → in_progress`). Гость уже прочитал
«выполнена» — теперь узнаёт, что это была ошибка и заявкой занимаются. Только если
итог действительно ушёл: опоздавшее «выполнена» уведомитель итога сам пропускает
(`notifications.notify_guest_on_request_closed`), и поправлять тогда нечего.

Адрес, язык и доставка — как у итога (`notifications.py`): диалог-источник заявки
(`request_origins`), канонический русский текст и один вызов перевода, деградация —
канонический текст; telegram — push, web — запись исходящего. Отдельным файлом:
`notifications.py` и без него за границей R-3. Заявки форм (заказ, бронь, АВР,
витрина) домен вернуть не даёт — их шаги гостю ведут свои подписчики.
"""

from __future__ import annotations

from typing import Final

from hospitality.ai.gateway.api import Deadline, LlmProvider
from hospitality.channels.common.store import (
    load_conversation_address,
    load_request_origin_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.notifications import (
    GUEST_DONE_KEY_TEMPLATE,
    NOTIFICATION_LLM_DEADLINE_SECONDS,
    _localize_for_guest,
    _tenant_default_language,
    guest_close_key,
)
from hospitality.channels.telegram.routing import current_correlation_id
from hospitality.modules.requests import api as requests_api
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Канонический русский текст поправки (spec 0080 §4); гостю — переводом на его язык.
GUEST_REOPENED_TEXT: Final = (
    "Простите, мы поторопились: заявка «{summary}» ещё не выполнена. Мы уже занимаемся ею."
)


def register(*, sender: TelegramSender, translate_provider: LlmProvider | None = None) -> None:
    """Подписать поправку на `request.status_changed` — зовёт `hospitality/worker.py`."""

    async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
        await notify_guest_on_request_reopened(
            event, sender=sender, translate_provider=translate_provider
        )

    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)


async def notify_guest_on_request_reopened(
    event: requests_api.RequestStatusChanged,
    *,
    sender: TelegramSender,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Поправка гостю, получившему «выполнена» перед возвратом в работу.

    Ключ поправки — номер возврата (`reopen_count`): повтор доставки события её не
    дублирует, а второй возврат той же заявки — новая поправка к новому «выполнена».
    """
    if not (
        event.old_status is requests_api.RequestStatus.DONE
        and event.new_status is requests_api.RequestStatus.IN_PROGRESS
    ):
        return
    conversation_id = await load_request_origin_conversation(event.request_id)
    if conversation_id is None:
        logger.info("guest_reopen_notice_skipped_no_origin", request_id=str(event.request_id))
        return
    request = await requests_api.get_request(event.request_id)
    if request.reopen_count < 1:  # pragma: no cover — событие пишет только reopen_request
        return
    # Итог закрытия, которое отменили, — его ключ на номер возврата раньше.
    closed_key = guest_close_key(GUEST_DONE_KEY_TEMPLATE, request.id, request.reopen_count - 1)
    if not await notification_already_sent(closed_key):
        logger.info("guest_reopen_notice_skipped_not_notified", request_id=str(request.id))
        return
    idempotency_key = f"guest:request_reopened:{request.id}:{request.reopen_count}"
    if await notification_already_sent(idempotency_key):
        logger.info("guest_reopen_notice_skipped_duplicate", request_id=str(request.id))
        return
    address = await load_conversation_address(conversation_id)
    if address is None:  # pragma: no cover — привязка ссылается на существующий диалог
        return
    channel, chat_id = address
    target_language = request.guest_language or await _tenant_default_language()
    text, translated = await _localize_for_guest(
        GUEST_REOPENED_TEXT.format(summary=request.summary),
        target_language,
        translate_provider,
        Deadline.after(NOTIFICATION_LLM_DEADLINE_SECONDS),
    )
    sent_id = await sender.send_message(chat_id, text) if channel == CHANNEL else None
    # Без `request_id`: под поправкой веб-чат кнопок оценки не ставит (spec 0076 §4.1).
    await record_outbound_message(
        conversation_id,
        text,
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "guest_notified_reopened",
        request_id=str(request.id),
        reopen_count=request.reopen_count,
        channel=channel,
        guest_language=target_language,
        translated=translated,
    )

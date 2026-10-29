"""Сообщения гостю в веб-чат о шагах заказа в номер (spec 0038 §5, Р-8).

Подписчик событий заявки: `request.created` — «Оформлен», `request.status_changed`
`new → in_progress` — «Принял», `request.payment_marked` — «Оплачено», `done` —
«Доставлено», `cancelled` сотрудником — «Отменён» с причиной. Отмена гостем в
чат не пишется: он видит её на странице. Заявка не заказ — подписчик молчит:
общий уведомитель гостя (`channels/telegram/notifications.py`) заказ не трогает,
потому что заказа нет в `request_origins`.

Адрес — веб-диалог гостя по `guest_identity_id` заказа: веб-канал ведёт один
диалог на идентичность (`channels/web/service.py`). Сообщение — запись
исходящего `Message`; страница чата заберёт её опросом. Текст — статический, на
языке заказа, без модели. Ключ идемпотентности — на шаг
(`guest:room_service:<request_id>:<шаг>`): повтор доставки события, возврат
заказа в очередь и повторное «Принял» второго сообщения не дают (P-8).
"""

from __future__ import annotations

import enum
import uuid
from typing import Final

import structlog

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.web.menu.texts import PAGE_TEXTS
from hospitality.channels.web.menu.texts import text as page_text
from hospitality.channels.web.service import CHANNEL
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class OrderStep(enum.StrEnum):
    """Шаг заказа, о котором пишут гостю, — и часть ключа идемпотентности."""

    PLACED = "placed"
    ACCEPTED = "accepted"
    PAID = "paid"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


# (шаг, каталог) → (ru, kk, en). Русские утверждены (spec 0038 §5), en и kk
# показаны основателю до кода и утверждены им 28.10.
_MESSAGES: Final[dict[tuple[OrderStep, str], tuple[str, str, str]]] = {
    (OrderStep.PLACED, "restaurant"): (
        "Заказ #{n} отправлен в ресторан: {items}. {total_phrase} Как только ресторан примет "
        "заказ, сотрудник придёт к вам за оплатой — наличными или картой. Готовить начнём "
        "после оплаты.",
        "№{n} тапсырыс мейрамханаға жіберілді: {items}. {total_phrase} Мейрамхана тапсырысты "
        "қабылдаған соң, қызметкер сізге төлемге келеді — қолма-қол немесе картамен. "
        "Төлемнен кейін дайындай бастаймыз.",
        "Order #{n} has been sent to the restaurant: {items}. {total_phrase} Once the restaurant "
        "accepts it, a staff member will come to you for payment — cash or card. We start "
        "cooking after payment.",
    ),
    (OrderStep.PLACED, "shop"): (
        "Заказ #{n} отправлен в магазин отеля: {items}. {total_phrase} Сотрудник придёт к вам — "
        "оплата наличными или картой, заказ отдадим после оплаты.",
        "№{n} тапсырыс қонақүй дүкеніне жіберілді: {items}. {total_phrase} Қызметкер сізге "
        "келеді — қолма-қол немесе картамен төлейсіз, тапсырысты төлемнен кейін береміз.",
        "Order #{n} has been sent to the hotel shop: {items}. {total_phrase} A staff member will "
        "come to you — pay by cash or card, and we'll hand over the order after payment.",
    ),
    (OrderStep.ACCEPTED, "restaurant"): (
        "Ресторан принял заказ #{n}. Сотрудник скоро подойдёт за оплатой — {total}, наличными "
        "или картой.",
        "Мейрамхана №{n} тапсырысты қабылдады. Қызметкер жақында төлемге келеді — {total}, "
        "қолма-қол немесе картамен.",
        "The restaurant has accepted order #{n}. A staff member will come shortly for payment — "
        "{total}, cash or card.",
    ),
    (OrderStep.ACCEPTED, "shop"): (
        "Магазин принял заказ #{n}. Сотрудник скоро подойдёт — {total}, наличными или картой.",
        "Дүкен №{n} тапсырысты қабылдады. Қызметкер жақында келеді — {total}, қолма-қол немесе "
        "картамен.",
        "The shop has accepted order #{n}. A staff member will come shortly — {total}, cash or "
        "card.",
    ),
    (OrderStep.PAID, "restaurant"): (
        "Оплата получена, готовим. Принесём в номер {room}.",
        "Төлем алынды, дайындап жатырмыз. {room} нөмірге әкелеміз.",
        "Payment received — we're cooking. We'll bring it to room {room}.",
    ),
    (OrderStep.PAID, "shop"): (
        "Оплата за заказ #{n} получена, спасибо!",
        "№{n} тапсырыс үшін төлем алынды, рақмет!",
        "Payment for order #{n} received, thank you!",
    ),
    (OrderStep.DELIVERED, "restaurant"): (
        "Заказ #{n} доставлен. Приятного аппетита!",
        "№{n} тапсырыс жеткізілді. Ас болсын!",
        "Order #{n} has been delivered. Enjoy your meal!",
    ),
    (OrderStep.DELIVERED, "shop"): (
        "Заказ #{n} доставлен. Хорошего отдыха!",
        "№{n} тапсырыс жеткізілді. Демалысыңыз жақсы өтсін!",
        "Order #{n} has been delivered. Enjoy your stay!",
    ),
}
_CANCELLED: Final = (
    "Заказ #{n} отменён: {reason}. {refund}Если это ошибка — напишите нам.",
    "№{n} тапсырыс болдырылмады: {reason}. {refund}Қате болса — бізге жазыңыз.",
    "Order #{n} has been cancelled: {reason}. {refund}If this is a mistake, write to us.",
)
_TOTAL_WITH_CHARGE: Final = (
    "Итого {total} с сервисным сбором.",
    "Қызмет ақысымен барлығы {total}.",
    "Total {total} including service charge.",
)
_TOTAL_PLAIN: Final = ("Итого {total}.", "Барлығы {total}.", "Total {total}.")
# Приписка к «Доставлено» с примечанием персонала (spec 0021 П-4).
_STAFF_NOTE: Final = ("От персонала: ", "Персоналдан: ", "Note from staff: ")


def register() -> None:
    """Подключить подписчика к событиям заявки — зовёт `hospitality/worker.py`."""
    subscribe(requests_api.RequestCreated, on_request_created)
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)
    subscribe(requests_api.RequestPaymentMarked, on_request_payment_marked)


async def on_request_created(event: requests_api.RequestCreated) -> None:
    await notify_order_step(event.request_id, OrderStep.PLACED)


async def on_request_payment_marked(event: requests_api.RequestPaymentMarked) -> None:
    await notify_order_step(event.request_id, OrderStep.PAID)


async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
    status = requests_api.RequestStatus
    if event.old_status is status.NEW and event.new_status is status.IN_PROGRESS:
        await notify_order_step(event.request_id, OrderStep.ACCEPTED)
    elif event.new_status is status.DONE:
        await notify_order_step(event.request_id, OrderStep.DELIVERED)
    elif event.new_status is status.CANCELLED:
        if event.initiator is requests_api.RequestInitiator.GUEST:
            return  # свою отмену гость видит на странице (spec 0038 §5)
        await notify_order_step(event.request_id, OrderStep.CANCELLED)


async def notify_order_step(request_id: uuid.UUID, step: OrderStep) -> None:
    """Записать гостю сообщение о шаге заказа — один раз на шаг."""
    order = await room_service_api.get_order_by_request(request_id)
    if order is None:
        return  # заявка не заказ
    identity = await room_service_api.order_guest_identity(request_id)
    if identity is None:
        logger.info("room_service_message_skipped_no_guest", request_id=str(request_id))
        return
    key = f"guest:room_service:{request_id}:{step.value}"
    if await notification_already_sent(key):
        logger.info("room_service_message_skipped_duplicate", request_id=str(request_id))
        return
    note = None
    if step is OrderStep.DELIVERED:
        # Примечание к «Готово» (частичное выполнение, spec 0021 П-4) не теряется:
        # приписка как есть, без модели (ревью PR #497, Н-9).
        note = (await requests_api.get_request(request_id)).resolution_note
    conversation_id = await ensure_conversation(CHANNEL, str(identity), guest_identity_id=identity)
    await record_outbound_message(
        conversation_id,
        compose_order_message(order, step, note=note),
        _correlation_id(),
        external_message_id=None,
        idempotency_key=key,
    )
    logger.info(
        "room_service_message_recorded",
        request_id=str(request_id),
        step=step.value,
        catalog=order.catalog.value,
        guest_language=order.guest_language,
    )


def compose_order_message(
    order: room_service_api.OrderRead, step: OrderStep, *, note: str | None = None
) -> str:
    """Текст шага заказа на языке заказа (spec 0038 §5)."""
    language = order.guest_language
    catalog = order.catalog.value
    total = room_service_api.format_amount(order.total_minor, language)
    number = order.number if order.number is not None else ""
    if step is OrderStep.CANCELLED:
        refund = page_text("text_refund", language) + " " if order.paid_at is not None else ""
        return _pick(_CANCELLED, language).format(
            n=number, reason=page_text(_reason_key(order), language), refund=refund
        )
    items = ", ".join(f"{line.names.text_for(language)} ×{line.quantity}" for line in order.lines)
    total_phrase = _pick(
        _TOTAL_WITH_CHARGE if order.service_charge_minor > 0 else _TOTAL_PLAIN, language
    ).format(total=total)
    text = _pick(_MESSAGES[(step, catalog)], language).format(
        n=number,
        items=items,
        total=total,
        total_phrase=total_phrase,
        room=order.room_number or "",
    )
    if note:
        text += "\n" + _pick(_STAFF_NOTE, language) + note
    return text


def _reason_key(order: room_service_api.OrderRead) -> str:
    """Ключ причины отмены в `texts.py`; не названа (или «гость сам») — «Другое» (Н-8)."""
    catalog = order.catalog.value
    reason = order.cancel_reason
    if reason in (None, room_service_api.OrderCancelReason.BY_GUEST):
        return f"reason_other_{catalog}"
    key = f"reason_{reason}"
    return key if key in PAGE_TEXTS else f"{key}_{catalog}"


def _pick(texts: tuple[str, str, str], language: str) -> str:
    ru, kk, en = texts
    return {"ru": ru, "kk": kk}.get(language, en)


def _correlation_id() -> str:
    """correlation_id события — его восстановил доставщик outbox (§10.2)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""

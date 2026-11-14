"""Состав реестра подписчиков после `register_subscribers` воркера (#1052).

Тесты подписчиков регистрируют их сами, мимо воркера, а сквозной тест воркера
(`test_worker.py`) доставляет только канарейку: потерянная строка в
`register_subscribers` проходила зелёной — событие тихо никому не доставлялось.
Новый подписчик в воркере — строка и здесь.
"""

from __future__ import annotations

from hospitality.channels.telegram.client import build_telegram_sender
from hospitality.shared import events
from hospitality.shared.config import get_settings
from hospitality.worker import register_subscribers

EXPECTED_SUBSCRIBERS = {
    "act_request.submitted": ["hospitality.ai.act_requisites.handle_act_request_submitted"],
    "canary.created": ["hospitality.platform.events.echo_canary_created"],
    "conversation.escalated": [
        "hospitality.channels.telegram.notifications.register.<locals>.on_conversation_escalated",
    ],
    "request.created": [
        "hospitality.channels.telegram.notifications.register.<locals>.on_request_created",
        "hospitality.channels.web.menu.messages.on_request_created",
        "hospitality.channels.web.table.messages.on_request_created",
    ],
    "request.payment_marked": ["hospitality.channels.web.menu.messages.on_request_payment_marked"],
    "request.status_changed": [
        "hospitality.channels.telegram.notifications.register.<locals>.on_request_status_changed",
        "hospitality.channels.telegram.reopen_notice.register.<locals>.on_request_status_changed",
        "hospitality.channels.web.documents.messages.on_request_status_changed",
        "hospitality.channels.web.menu.messages.on_request_status_changed",
        "hospitality.channels.web.table.messages.on_request_status_changed",
        "hospitality.modules.table_booking.reopen.withdraw_answer_on_reopen",
    ],
    "staff.superadmin_acted": [
        "hospitality.platform.superadmin_alerts.build_superadmin_alert_handler"
        ".<locals>.notify_superadmin_acted",
    ],
    "telegram_reply.undelivered": [
        "hospitality.channels.telegram.redelivery.register.<locals>.on_telegram_reply_undelivered",
    ],
}


def test_worker_registers_every_subscriber() -> None:
    # Реестр глобальный; после теста его восстановит `_isolated_event_subscribers`.
    events._subscribers.clear()
    events._event_types.clear()

    register_subscribers(build_telegram_sender(get_settings()), alert_sender=None)

    registered = {
        name: sorted(f"{handler.__module__}.{handler.__qualname__}" for handler in handlers)
        for name, handlers in events._subscribers.items()
    }
    assert registered == EXPECTED_SUBSCRIBERS

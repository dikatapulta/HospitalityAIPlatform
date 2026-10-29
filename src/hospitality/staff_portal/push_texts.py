"""Слова уведомлений на телефон (spec 0050 §7) — те же, что на компьютере.

Уведомление на компьютере собирает `static/signal.js` (`notify`, `notifyCall`),
на телефоне — эти функции; правятся вместе (P-12), сверку держит
`tests/test_push_sweep.py`. Одно отличие — замечание при проверке номера: его
текст на экран блокировки не выносится (на нём может быть пересказ гостя),
телефон зовёт открыть доску. Метки (`tag`) — те же, что у вкладки: новое
уведомление о том же заменяет прежнее, а не копится.
"""

from __future__ import annotations

from typing import Final

from hospitality.platform.staff_push import PushMessage
from hospitality.staff_portal.signal import TurnoverCallView, UnclaimedRequestView

_CALL_TITLES: Final = {"inspect": " — выезд", "accept": " убран", "remark": " — замечание"}
_CALL_BODIES: Final = {
    "inspect": "Проверьте номер",
    "accept": "Примите номер",
    "remark": "Откройте доску «Выезды и уборка»",
}


def _number(item: UnclaimedRequestView) -> str:
    return "" if item.daily_number is None else f" #{item.daily_number}"


def _place(item: UnclaimedRequestView) -> str:
    if item.room_number:
        return f"Комната {item.room_number} · {item.category_name}"
    return item.category_name


def new_request(item: UnclaimedRequestView, url: str) -> PushMessage:
    """Новая заявка; уже поднятая к ресепшену несрочная — «Никто не взял»."""
    if item.is_raised and not item.is_urgent:
        return raised_request(item, url)
    if item.is_urgent:
        title = "🚨 Срочная заявка"
    elif item.is_order:
        title = "Новый заказ"
    else:
        title = "Новая заявка"
    return PushMessage(
        title=title + _number(item), body=_place(item), url=url, tag=f"request-{item.id}-new"
    )


def raised_request(item: UnclaimedRequestView, url: str) -> PushMessage:
    """Третья ступень лестницы (spec 0045 §4): служба не взяла за два срока."""
    return PushMessage(
        title="Никто не взял заявку" + _number(item),
        body=f"{_place(item)} · ждёт {item.age_label}",
        url=url,
        tag=f"request-{item.id}-raised",
    )


def overdue_request(item: UnclaimedRequestView, url: str) -> PushMessage:
    return PushMessage(
        title=f"Заявка{_number(item)} ждёт уже {item.age_label}",
        body=_place(item),
        url=url,
        tag=f"request-{item.id}-overdue",
    )


def turnover_call(item: TurnoverCallView, url: str) -> PushMessage:
    """Номер доски «Выезды и уборка» (spec 0046 §7)."""
    return PushMessage(
        title=item.room_number + _CALL_TITLES[item.kind],
        body=_CALL_BODIES[item.kind],
        url=url,
        tag=f"turnover-{item.id}-{item.kind}",
    )

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
from hospitality.staff_portal.signal import (
    StalledRequestView,
    TurnoverCallView,
    UnclaimedRequestView,
)

# Запрос из 3D-витрины (spec 0069 §5) — тем же словом, что заголовок карточки.
_SHOWROOM_TITLES: Final = {"group": "Групповой запрос", "booking": "Запрос на бронирование"}
_CALL_TITLES: Final = {
    "inspect": " — выезд",
    "accept": " убран",
    "remark": " — замечание",
    "extended": " — продлили",
}
# Продление (spec 0062 §5) — без даты: срок проживания — данные гостя, а политика
# (§6) называет в уведомлении через службы Google и Apple только номер и службу.
_CALL_BODIES: Final = {
    "inspect": "Проверьте номер",
    "accept": "Примите номер",
    "remark": "Откройте доску «Выезды и уборка»",
    "extended": "Гость остаётся — выезда сегодня не будет",
}


def _number(item: UnclaimedRequestView | StalledRequestView) -> str:
    return "" if item.daily_number is None else f" #{item.daily_number}"


def _place(item: UnclaimedRequestView | StalledRequestView) -> str:
    if item.room_number:
        return f"Комната {item.room_number} · {item.category_name}"
    return item.category_name


def new_request(item: UnclaimedRequestView, url: str) -> PushMessage:
    """Новая заявка; уже поднятая несрочная — «Гость напоминает…» или «Никто не взял»."""
    if item.is_guest_raised and not item.is_urgent:
        return guest_reminder(item, url)
    if item.is_raised and not item.is_urgent:
        return raised_request(item, url)
    if item.is_urgent:
        title = "🚨 Срочная заявка"
    elif item.is_order:
        title = "Новый заказ"
    elif item.is_booking:
        title = "Запрос на столик"
    elif item.showroom_kind is not None:
        title = _SHOWROOM_TITLES.get(item.showroom_kind, "Новая заявка")
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


def guest_reminder_title(count: int, daily_number: int | None) -> str:
    """«Гость напоминает второй раз: #12» (spec 0070 §7); поднятая с первого
    напоминания (уже просрочена) — «Гость напоминает о заявке #12»."""
    if count <= 1:
        return "Гость напоминает о заявке" + ("" if daily_number is None else f" #{daily_number}")
    head = "Гость напоминает второй раз" if count == 2 else f"Гость напоминает уже {count}-й раз"
    return head + ("" if daily_number is None else f": #{daily_number}")


def guest_reminder(item: UnclaimedRequestView | StalledRequestView, url: str) -> PushMessage:
    """Напоминания гостя подняли заявку (spec 0070 §5). Сути заявки нет: экран
    блокировки видят посторонние, суть — в карточке."""
    if isinstance(item, StalledRequestView):
        wait = f"в работе {item.in_work_label}"
    else:
        wait = f"ждёт {item.age_label}"
    return PushMessage(
        title=guest_reminder_title(item.guest_reminders, item.daily_number),
        body=f"{_place(item)} · {wait}",
        url=url,
        tag=f"request-{item.id}-guest",
    )


def overdue_request(item: UnclaimedRequestView, url: str) -> PushMessage:
    return PushMessage(
        title=f"Заявка{_number(item)} ждёт уже {item.age_label}",
        body=_place(item),
        url=url,
        tag=f"request-{item.id}-overdue",
    )


def stalled_request(item: StalledRequestView, url: str) -> PushMessage:
    """Взятая и не закрытая дольше срока отеля (spec 0028 §8): забыли «Готово» или бросили."""
    return PushMessage(
        title=f"Заявка{_number(item)} в работе уже {item.in_work_label}",
        body=f"{_place(item)} · сделано — нажмите «Готово»",
        url=url,
        tag=f"request-{item.id}-stalled",
    )


def turnover_call(item: TurnoverCallView, url: str) -> PushMessage:
    """Номер доски «Выезды и уборка» (spec 0046 §7); «срочно к часу» и продление —
    spec 0062 §5."""
    if item.kind == "urgent":
        title = f"{item.room_number} — срочно к {item.needed_by}"
        body = "Номер убран — примите его первым" if item.cleaned else "Возьмите этот номер первым"
    else:
        title, body = item.room_number + _CALL_TITLES[item.kind], _CALL_BODIES[item.kind]
    return PushMessage(title=title, body=body, url=url, tag=f"turnover-{item.id}-{item.kind}")

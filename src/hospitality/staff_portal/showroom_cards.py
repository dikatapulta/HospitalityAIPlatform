"""Карточка запроса из 3D-витрины в очереди кабинета (spec 0069 §2.4, §5; PR (в), #805).

Запрос — обычная заявка службы продаж или бронирования: «Взять», «Готово»
(= «ответили»), «Передать» и «Отменить» — общие. Своё у него — заголовок
«Групповой запрос · Silk Road Travel · 32 гост. · 12–15 марта» (номер `#N` — в
строке под ним, знак — SVG «cube», не эмодзи: дизайн-ревью #759), строки
контакта, номеров или желания и пожеланий — из модуля `showroom` (контактов в
тексте заявки нет, у них свой срок, spec 0069 §3) — и подсказка, как отвечать.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api

Kind = showroom_api.ShowroomRequestKind

_TITLES: Final[dict[Kind, str]] = {
    Kind.GROUP: "Групповой запрос",
    Kind.BOOKING: "Запрос на бронирование",
}
ANSWER_HINT: Final = (
    "Ответьте по телефону или почте. «Готово» — когда ответили. "
    "Наличие и бронь — в Opera, как обычно."
)
ANONYMIZED_LINE: Final = "Контакты удалены по сроку хранения."


def with_showroom(
    card: dict[str, Any],
    showroom: showroom_api.StaffShowroomRequestRead | None,
    request: requests_api.ServiceRequestRead,
) -> dict[str, Any]:
    """Запросу витрины — свой заголовок и строки; общие кнопки остаются."""
    if showroom is None:
        return card | {"showroom": None}
    fields = showroom_card(showroom, request)
    # Номера и желание — уже в строках карточки: `details` заявки их повторил бы.
    return card | {"showroom": fields, "summary": fields["title"], "details": None}


def showroom_card(
    showroom: showroom_api.StaffShowroomRequestRead, request: requests_api.ServiceRequestRead
) -> dict[str, Any]:
    kind_title = _TITLES[showroom.kind]
    number = f" #{request.daily_number}" if request.daily_number is not None else ""
    dates = showroom_api.date_range_ru(showroom.check_in, showroom.check_out)
    guests = f"{showroom.guests_count} гост."
    wish = showroom_api.wish_ru(
        showroom.room_numbers, showroom.wish_view_names, showroom.wish_type_names
    )
    parts: tuple[str | None, ...] = (
        (kind_title, showroom.company, guests, dates)
        if showroom.kind is Kind.GROUP
        else (kind_title, dates, guests, wish)
    )
    return {
        "title": " · ".join(part for part in parts if part),
        "panel_title": kind_title + number,
        "lines": _lines(showroom, wish),
        "hint": ANSWER_HINT,
    }


def _lines(showroom: showroom_api.StaffShowroomRequestRead, wish: str | None) -> list[str]:
    """«Контакт: …», «Номера: …» или «Желание: …», «Пожелания: …» (§5)."""
    lines: list[str] = []
    if showroom.anonymized:
        lines.append(ANONYMIZED_LINE)
    else:
        contact = ", ".join(
            part for part in (showroom.contact_name, showroom.phone, showroom.email) if part
        )
        if contact:
            lines.append(f"Контакт: {contact}")
    if showroom.room_numbers:
        lines.append(f"Номера: {', '.join(showroom.room_numbers)}")
    elif wish:
        lines.append(f"Желание: {wish}")
    if showroom.wishes:
        lines.append(f"Пожелания: {showroom.wishes}")
    return lines

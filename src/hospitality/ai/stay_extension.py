"""Блок хода «Staying longer»: свободные номера на ночи после выезда гостя (spec 0059 §2.4).

Что бот знает о продлении, решает `guests_api.get_extension_outlook` (ночи без
мест, до какой ночи видит файл Opera); здесь это становится текстом для модели.
Блок — динамический контекст хода, как комната и заявки в оркестраторе, а не
новая версия файла промпта и не инструмент: ход однократный, и «проверить и
ответить» инструментом потребовало бы второго обращения к модели (§2.4).

Пустая строка — блока нет, бот ведёт себя как v1 — у гостя без привязки к Stay
(Telegram, #484), без конфига тенанта и всюду, где outlook пуст (§2.3). Текст
блока пересказан в spec 0059 §2.4: правишь здесь — правишь там тем же PR.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta, tzinfo

from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import TenantConfig

# Имена — свои, а не `%A`/`%B` strftime: те зависят от локали процесса.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


async def stay_extension_block(stay_id: uuid.UUID | None, config: TenantConfig | None) -> str:
    """Блок для гостя с привязкой к Stay; outlook пуст или данных хода нет — пустая строка.

    `config` — то же чтение конфига, что у хода оркестратора: по нему пояс отеля
    для часа обновления файла. Правило «Opera или нет» здесь не повторяется —
    его владелец `get_extension_outlook`.
    """
    if stay_id is None or config is None:
        return ""
    outlook = await guests_api.get_extension_outlook(stay_id)
    return render_stay_extension_block(outlook, config.tzinfo)


def render_stay_extension_block(
    outlook: guests_api.StayExtensionOutlook | None, zone: tzinfo
) -> str:
    """Текст блока: даты — по поясу отеля, ночь — «4–5 November» (с вечера 4-го на 5-е).

    Ночи без мест перечислены по одной, а не диапазоном: «4–7 November» модель
    прочла бы и как ночи, и как дни.
    """
    if outlook is None:
        return ""
    if outlook.full_nights:
        full = "Nights with NO free rooms: " + ", ".join(map(_night, outlook.full_nights)) + "."
    else:
        full = "Every one of these nights has free rooms."
    return (
        "\n\n# Staying longer\n\n"
        f"Today at the hotel is {_day(outlook.today)}. This guest's checkout is "
        f"{_day(outlook.departure)} at {outlook.checkout_hour:02d}:00.\n\n"
        "The hotel's booking system says how many rooms are free each night up to the "
        f"night of {_night(outlook.known_through, year=True)} "
        f"({_updated(outlook, zone)}). {full}\n\n"
        "Use this only when the guest asks to stay longer than their checkout. Do not "
        "bring it up yourself: if the guest has not asked to stay longer, say nothing "
        "about free rooms or nights. Tell them whether the hotel has free rooms for the "
        'nights they ask about — as "very likely", never as certain: the reception desk '
        "confirms the extension, the room and the price. Never say that the stay is "
        "extended, and never name a price. You know nothing about nights after "
        f"{_night(outlook.known_through)}, about whether their own room stays free, or "
        "about a later checkout hour on the last day — for those, say the reception desk "
        "will check. "
        "Example: a guest who asks to stay one more night, when that night has free "
        "rooms, hears that there are very likely free rooms and that the reception desk "
        "will confirm the extension and the price."
    )


def _day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]} {day.year}"


def _night(night: date, *, year: bool = False) -> str:
    """«4–5 November»; на стыке месяцев — «30 November–1 December»; с годом — у утра."""
    morning = night + timedelta(days=1)
    end = f"{morning.day} {_MONTHS[morning.month - 1]}" + (f" {morning.year}" if year else "")
    if morning.month == night.month:
        return f"{night.day}–{end}"
    start = f"{night.day} {_MONTHS[night.month - 1]}"
    if year and morning.year != night.year:
        start += f" {night.year}"
    return f"{start}–{end}"


def _updated(outlook: guests_api.StayExtensionOutlook, zone: tzinfo) -> str:
    local = outlook.updated_at.astimezone(zone)
    when = (
        "today" if local.date() == outlook.today else f"on {local.day} {_MONTHS[local.month - 1]}"
    )
    return f"updated {when} at {local:%H:%M}"

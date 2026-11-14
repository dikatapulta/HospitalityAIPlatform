"""Срок письменного объяснения (spec 0077 §6.3): два рабочих дня после дня получения.

День получения не считается; срок — конец второго рабочего дня по поясу отеля.
Рабочие дни — понедельник–пятница минус `TenantConfig.holidays`. Из прочтений
ТК РК ст. 65 п. 2 берётся самое позднее: раньше срока «истёк» система не скажет.
Срок считается один раз — нажатием «Получил(а)» — и хранится в `memo_demands.due_at`:
праздник, вписанный позже, обещанную сотруднику дату не сдвигает.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Final

WORKING_DAYS: Final = 2
_SATURDAY: Final = 5


def is_working_day(day: date, holidays: Collection[date]) -> bool:
    return day.weekday() < _SATURDAY and day not in holidays


def explanation_due(received_at: datetime, zone: tzinfo, holidays: Collection[date]) -> datetime:
    """Последний миг второго рабочего дня после дня получения, в UTC."""
    day = received_at.astimezone(zone).date()
    left = WORKING_DAYS
    while left:
        day += timedelta(days=1)
        if is_working_day(day, holidays):
            left -= 1
    return datetime.combine(day, time.max, tzinfo=zone).astimezone(UTC)

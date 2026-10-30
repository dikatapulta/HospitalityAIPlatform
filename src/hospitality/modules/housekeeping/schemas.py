"""Pydantic-схемы границ модуля housekeeping (spec 0046, R-6, P-7) — копия канона
`modules/requests/schemas.py`: наружу уходят `*Read`, ORM-объекты — никогда.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from hospitality.modules.housekeeping.models import TurnoverStatus
from hospitality.platform.staff_actions import StaffActor

# Личность сотрудника кабинета — та же, что у заявок и Stay (spec 0041 §2.3).
ActingUser = StaffActor


class RoomTurnoverRead(BaseModel):
    """Путь номера после выезда: статус и «кто/когда» каждого пройденного шага."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    room_number: str
    status: TurnoverStatus
    inspection_note: str | None
    reported_at: datetime
    reported_by_display_name: str | None
    inspected_at: datetime | None
    inspected_by_display_name: str | None
    cleaning_started_at: datetime | None
    cleaning_started_by_display_name: str | None
    cleaned_at: datetime | None
    cleaned_by_display_name: str | None
    ready_at: datetime | None
    accepted_by_display_name: str | None
    cancelled_at: datetime | None
    cancelled_by_display_name: str | None


class TurnoverReport(BaseModel):
    """Итог «Отметить выезд»: путь номера и создан ли он сейчас.

    `created=False` — у номера уже был открытый путь, он и вернулся (повторное
    нажатие, второй ресепшенист): страница скажет «уже у хаускипинга».
    """

    turnover: RoomTurnoverRead
    created: bool


class CleanedByStaff(BaseModel):
    """Строка «Сегодня убрано»: сколько номеров отметил «Убрано» один сотрудник."""

    display_name: str
    count: int


class FloorAssignmentRead(BaseModel):
    """Строка раздачи этажей (spec 0053 §6): этаж, который дали сотруднику, и когда."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    floor: int
    assigned_at: datetime

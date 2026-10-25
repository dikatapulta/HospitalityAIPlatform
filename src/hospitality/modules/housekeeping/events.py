"""Доменные события модуля housekeeping (spec 0046 §3, P-6) — копия канона
`platform/events.py`.

Публикуются в одной транзакции с бизнес-записью. Подписчиков пока нет (воркер
помечает доставку `handlers=0` — штатно): события — вход будущей синхронизации
статуса номера с OPERA через порт PMS (NG-2, ADR-021). Имён сотрудников и
замечания в payload нет — только номер и статусы.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from hospitality.modules.housekeeping.models import TurnoverStatus
from hospitality.shared.events import DomainEvent


class RoomTurnoverReported(DomainEvent):
    """Факт «ресепшен отметил выезд из номера» — в транзакции `report_checkout`."""

    event_name: ClassVar[str] = "room_turnover.reported"

    turnover_id: uuid.UUID
    room_number: str


class RoomTurnoverStatusChanged(DomainEvent):
    """Шаг пути номера — в транзакции `change_turnover_status`."""

    event_name: ClassVar[str] = "room_turnover.status_changed"

    turnover_id: uuid.UUID
    room_number: str
    old_status: TurnoverStatus
    new_status: TurnoverStatus

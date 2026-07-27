"""Доменные события модуля guests (spec 0027 §1.5, P-6) — копия канона
`platform/events.py`.

Публикуются в одной транзакции с бизнес-записью. Подписчиков в серии 0027
нет (воркер помечает доставку `handlers=0` — штатно): события фиксируют факт
для будущего кабинета персонала (#48) и политик обслуживания. Кода заселения
в payload нет ни в каком виде (в БД и событиях — никогда, ADR-008).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from hospitality.shared.events import DomainEvent


class StayCheckedIn(DomainEvent):
    """Факт «гость заселён» — публикуется в транзакции `check_in`."""

    event_name: ClassVar[str] = "stay.checked_in"

    stay_id: uuid.UUID
    room_number: str


class StayCheckedOut(DomainEvent):
    """Факт «гость выехал» — публикуется в транзакции `check_out`."""

    event_name: ClassVar[str] = "stay.checked_out"

    stay_id: uuid.UUID
    room_number: str

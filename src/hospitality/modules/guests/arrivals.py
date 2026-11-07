"""Заселения за отрезок времени — список кабинета «Заселены за день» (#832).

Взгляд персонала в контексте тенанта запроса (P-4, RLS): Stay любого статуса,
заселённые в полуинтервале `[start, end)`, и подключался ли к ним гость.
Границы дня считает кабинет по поясу отеля (FOUNDATION §9) — домен отдаёт факты.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import exists, select

from hospitality.modules.guests.models import GuestSession, Stay
from hospitality.modules.guests.schemas import StayArrival, StayRead
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Потолок списка за день: у отеля на 350 номеров — 100–150 заселений в день,
# запас двукратный. Упёрлись — предупреждение в лог: усечённый список выглядел бы полным.
_ARRIVALS_LIST_LIMIT: Final = 300


async def list_stays_checked_in_between(start: datetime, end: datetime) -> list[StayArrival]:
    """Stay с `check_in_at` в `[start, end)` — свежие сверху, любого статуса.

    `guest_connected` — есть хоть одна сессия Stay, в том числе погашенная
    выездом (см. `StayArrival`).
    """
    connected = exists().where(GuestSession.stay_id == Stay.id)
    async with session_scope() as session:
        rows = await session.execute(
            select(Stay, connected)
            .where(Stay.check_in_at >= start, Stay.check_in_at < end)
            .order_by(Stay.check_in_at.desc(), Stay.room_number)
            .limit(_ARRIVALS_LIST_LIMIT)
        )
        arrivals = [
            StayArrival(stay=StayRead.model_validate(stay), guest_connected=bool(is_connected))
            for stay, is_connected in rows
        ]
    if len(arrivals) == _ARRIVALS_LIST_LIMIT:
        logger.warning("stay_arrivals_truncated", limit=_ARRIVALS_LIST_LIMIT)
    return arrivals

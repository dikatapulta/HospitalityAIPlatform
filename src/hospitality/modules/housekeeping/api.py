"""Публичный интерфейс модуля housekeeping (spec 0046, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: кабинет персонала и джоба ретеншна
импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны (контракт 5
import-linter). Здесь нет логики, только контракт.
"""

from __future__ import annotations

from hospitality.modules.housekeeping.events import (
    RoomTurnoverReported,
    RoomTurnoverStatusChanged,
)
from hospitality.modules.housekeeping.models import TurnoverStatus
from hospitality.modules.housekeeping.schemas import (
    ActingUser,
    CleanedByStaff,
    RoomTurnoverRead,
    TurnoverReport,
)
from hospitality.modules.housekeeping.service import (
    ERR_HOUSEKEEPING_INVALID_TRANSITION,
    ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND,
    OPEN_TURNOVER_STATUSES,
    TURNOVER_TRANSITIONS,
    change_turnover_status,
    clear_expired_inspection_notes,
    count_cleaned_since,
    get_turnover,
    list_open_turnovers,
    list_turnovers_ready_since,
    report_checkout,
)

__all__ = [
    "ERR_HOUSEKEEPING_INVALID_TRANSITION",
    "ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND",
    "OPEN_TURNOVER_STATUSES",
    "TURNOVER_TRANSITIONS",
    "ActingUser",
    "CleanedByStaff",
    "RoomTurnoverRead",
    "RoomTurnoverReported",
    "RoomTurnoverStatusChanged",
    "TurnoverReport",
    "TurnoverStatus",
    "change_turnover_status",
    "clear_expired_inspection_notes",
    "count_cleaned_since",
    "get_turnover",
    "list_open_turnovers",
    "list_turnovers_ready_since",
    "report_checkout",
]

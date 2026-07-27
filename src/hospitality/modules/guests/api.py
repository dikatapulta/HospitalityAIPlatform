"""Публичный интерфейс модуля guests (spec 0027, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: каналы (`channels/web`, будущий
auth-only Telegram), CLI заселения (`tools/checkin`) и другие доменные модули
импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны (контракт
import-linter). Здесь нет логики, только контракт. HTTP-роутера у модуля нет:
операции персонала до кабинета (#48) выполняет CLI, гостевые — канал web.
"""

from __future__ import annotations

from hospitality.modules.guests.events import StayCheckedIn, StayCheckedOut
from hospitality.modules.guests.models import GuestIdentityKind, StayStatus
from hospitality.modules.guests.schemas import (
    ActiveGuestSession,
    GuestSessionGrant,
    GuestSessionStart,
    StayCheckIn,
    StayCheckInResult,
    StayRead,
)
from hospitality.modules.guests.service import (
    ERR_GUESTS_CODE_REISSUE_CONFLICT,
    ERR_GUESTS_ROOM_OCCUPIED,
    ERR_GUESTS_STAY_NOT_FOUND,
    WEB_IDENTITY_KIND,
    check_in,
    check_out,
    find_active_stay,
    format_access_code,
    list_active_stays,
    reissue_access_code,
    resolve_session,
    start_guest_session,
)

__all__ = [
    "ERR_GUESTS_CODE_REISSUE_CONFLICT",
    "ERR_GUESTS_ROOM_OCCUPIED",
    "ERR_GUESTS_STAY_NOT_FOUND",
    "WEB_IDENTITY_KIND",
    "ActiveGuestSession",
    "GuestIdentityKind",
    "GuestSessionGrant",
    "GuestSessionStart",
    "StayCheckIn",
    "StayCheckInResult",
    "StayCheckedIn",
    "StayCheckedOut",
    "StayRead",
    "StayStatus",
    "check_in",
    "check_out",
    "find_active_stay",
    "format_access_code",
    "list_active_stays",
    "reissue_access_code",
    "resolve_session",
    "start_guest_session",
]

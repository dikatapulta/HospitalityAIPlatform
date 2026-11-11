"""Публичный интерфейс модуля guests (spec 0027, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: каналы (`channels/web`,
`channels/telegram` — auth-only, spec 0074), CLI заселения (`tools/checkin`)
и другие доменные модули импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны (контракт
import-linter). Здесь нет логики, только контракт. HTTP-роутера у модуля нет:
операции персонала до кабинета (#48) выполняет CLI, гостевые — канал web.
"""

from __future__ import annotations

from hospitality.modules.guests.arrivals import list_stays_checked_in_between
from hospitality.modules.guests.bindlink import (
    issue_bind_link,
    start_guest_session_by_bind_link,
)
from hospitality.modules.guests.consent_records import record_consent_evidence
from hospitality.modules.guests.events import StayCheckedIn, StayCheckedOut
from hospitality.modules.guests.expiry import (
    ERR_GUESTS_AUTO_CHECKOUT_FAILED,
    close_overdue_stays,
    list_overdue_stays,
    list_recent_extensions,
    list_stays_leaving_between,
)
from hospitality.modules.guests.extension import get_extension_outlook
from hospitality.modules.guests.identity_rooms import identities_in_room, rooms_for_identities
from hospitality.modules.guests.models import GuestIdentityKind, StaySource, StayStatus
from hospitality.modules.guests.pms_status import get_pms_status, missing_from_pms
from hospitality.modules.guests.pms_sync import (
    ERR_GUESTS_PMS_CLOSURES_HELD,
    ERR_GUESTS_PMS_SNAPSHOT_STALE,
    ERR_GUESTS_PMS_SYNC_FAILED,
    refresh_pms_snapshot_age_metrics,
    sync_stays_from_pms,
)
from hospitality.modules.guests.schemas import (
    MAX_GUESTS_PER_STAY,
    ROOM_NUMBER_PATTERN,
    ActiveGuestSession,
    GuestSessionBind,
    GuestSessionGrant,
    GuestSessionStart,
    PmsDiscrepancy,
    PmsDiscrepancyKind,
    PmsStatus,
    StayArrival,
    StayCheckIn,
    StayCheckInResult,
    StayDataRetentionStats,
    StayExtensionOutlook,
    StayRead,
)
from hospitality.modules.guests.service import (
    ERR_GUESTS_CHECK_OUT_IN_PAST,
    ERR_GUESTS_CODE_REISSUE_CONFLICT,
    ERR_GUESTS_ROOM_OCCUPIED,
    ERR_GUESTS_STAY_NOT_FOUND,
    WEB_IDENTITY_KIND,
    check_in,
    check_out,
    count_stay_sessions,
    extend_stay,
    find_active_stay,
    format_access_code,
    get_active_stay,
    list_active_stays,
    move_stay,
    reissue_access_code,
    resolve_identity_session,
    resolve_session,
    set_checkout_time,
    start_guest_session,
)
from hospitality.modules.guests.stay_retention import delete_expired_stay_data

__all__ = [
    "ERR_GUESTS_AUTO_CHECKOUT_FAILED",
    "ERR_GUESTS_CHECK_OUT_IN_PAST",
    "ERR_GUESTS_CODE_REISSUE_CONFLICT",
    "ERR_GUESTS_PMS_CLOSURES_HELD",
    "ERR_GUESTS_PMS_SNAPSHOT_STALE",
    "ERR_GUESTS_PMS_SYNC_FAILED",
    "ERR_GUESTS_ROOM_OCCUPIED",
    "ERR_GUESTS_STAY_NOT_FOUND",
    "MAX_GUESTS_PER_STAY",
    "ROOM_NUMBER_PATTERN",
    "WEB_IDENTITY_KIND",
    "ActiveGuestSession",
    "GuestIdentityKind",
    "GuestSessionBind",
    "GuestSessionGrant",
    "GuestSessionStart",
    "PmsDiscrepancy",
    "PmsDiscrepancyKind",
    "PmsStatus",
    "StayArrival",
    "StayCheckIn",
    "StayCheckInResult",
    "StayCheckedIn",
    "StayCheckedOut",
    "StayDataRetentionStats",
    "StayExtensionOutlook",
    "StayRead",
    "StaySource",
    "StayStatus",
    "check_in",
    "check_out",
    "close_overdue_stays",
    "count_stay_sessions",
    "delete_expired_stay_data",
    "extend_stay",
    "find_active_stay",
    "format_access_code",
    "get_active_stay",
    "get_extension_outlook",
    "get_pms_status",
    "identities_in_room",
    "issue_bind_link",
    "list_active_stays",
    "list_overdue_stays",
    "list_recent_extensions",
    "list_stays_checked_in_between",
    "list_stays_leaving_between",
    "missing_from_pms",
    "move_stay",
    "record_consent_evidence",
    "refresh_pms_snapshot_age_metrics",
    "reissue_access_code",
    "resolve_identity_session",
    "resolve_session",
    "rooms_for_identities",
    "set_checkout_time",
    "start_guest_session",
    "start_guest_session_by_bind_link",
    "sync_stays_from_pms",
]

"""CANONICAL: публичный интерфейс модуля requests (Task 0012, FOUNDATION §5.2, R-5).

Единственная точка входа в модуль снаружи: composition root (подключает
`router`, Task 0013), AI-инструменты (Task 0015) и другие доменные модули
импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны (контракт
import-linter). Здесь нет логики, только контракт: сервисные функции,
схемы границ, события, коды ошибок и HTTP-роутер.
"""

from __future__ import annotations

from hospitality.modules.requests.day_summary import (
    DayServiceCounts,
    RequestsDaySummary,
    day_summary,
)
from hospitality.modules.requests.details import append_request_details
from hospitality.modules.requests.direction import direct_request
from hospitality.modules.requests.events import (
    RequestCategoryChanged,
    RequestCreated,
    RequestInitiator,
    RequestPaymentMarked,
    RequestStatusChanged,
)
from hospitality.modules.requests.models import (
    CANCEL_REASON_LABELS,
    CancelReason,
    RequestStatus,
    ServiceRequestOrigin,
)
from hospitality.modules.requests.payment import mark_request_paid
from hospitality.modules.requests.quota import check_quota
from hospitality.modules.requests.remainder import complete_with_remainder
from hospitality.modules.requests.reminders import (
    list_guest_reminded_requests,
    record_guest_reminder,
)
from hospitality.modules.requests.router import router
from hospitality.modules.requests.schemas import (
    ActingUser,
    FloorScope,
    QuotaCheck,
    RemainderOutcome,
    RequestCategoryCreate,
    RequestCategoryRead,
    ServiceRequestCreate,
    ServiceRequestPage,
    ServiceRequestRead,
    ServiceRequestStatusUpdate,
)
from hospitality.modules.requests.sent import list_requests_sent_by_services
from hospitality.modules.requests.service import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_DONE_WITHOUT_PAYMENT,
    ERR_REQUESTS_FOREIGN_SERVICE,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    ERR_REQUESTS_TRANSFER_REJECTED,
    REQUEST_TEXT_ANONYMIZED_PLACEHOLDER,
    STATUS_TRANSITIONS,
    anonymize_expired_request_texts,
    change_request_status,
    create_category,
    create_request,
    find_open_requests_by_daily_number,
    get_request,
    list_categories,
    list_open_requests,
    list_open_requests_by_ids,
    list_requests,
    list_requests_by_ids,
    list_requests_closed_since,
    list_requests_created_since,
    list_stalled_requests,
    list_unclaimed_requests,
    rename_category,
    transfer_request,
)
from hospitality.modules.requests.staff_stats import StaffStatsRow, staff_stats

__all__ = [
    "CANCEL_REASON_LABELS",
    "ERR_REQUESTS_CATEGORY_KEY_TAKEN",
    "ERR_REQUESTS_CATEGORY_NOT_FOUND",
    "ERR_REQUESTS_DONE_WITHOUT_PAYMENT",
    "ERR_REQUESTS_FOREIGN_SERVICE",
    "ERR_REQUESTS_INVALID_STATUS_TRANSITION",
    "ERR_REQUESTS_PAYMENT_NOT_APPLICABLE",
    "ERR_REQUESTS_REQUEST_NOT_FOUND",
    "ERR_REQUESTS_TRANSFER_REJECTED",
    "REQUEST_TEXT_ANONYMIZED_PLACEHOLDER",
    "STATUS_TRANSITIONS",
    "ActingUser",
    "CancelReason",
    "DayServiceCounts",
    "FloorScope",
    "QuotaCheck",
    "RemainderOutcome",
    "RequestCategoryChanged",
    "RequestCategoryCreate",
    "RequestCategoryRead",
    "RequestCreated",
    "RequestInitiator",
    "RequestPaymentMarked",
    "RequestStatus",
    "RequestStatusChanged",
    "RequestsDaySummary",
    "ServiceRequestCreate",
    "ServiceRequestOrigin",
    "ServiceRequestPage",
    "ServiceRequestRead",
    "ServiceRequestStatusUpdate",
    "StaffStatsRow",
    "anonymize_expired_request_texts",
    "append_request_details",
    "change_request_status",
    "check_quota",
    "complete_with_remainder",
    "create_category",
    "create_request",
    "day_summary",
    "direct_request",
    "find_open_requests_by_daily_number",
    "get_request",
    "list_categories",
    "list_guest_reminded_requests",
    "list_open_requests",
    "list_open_requests_by_ids",
    "list_requests_by_ids",
    "list_requests",
    "list_requests_closed_since",
    "list_requests_created_since",
    "list_requests_sent_by_services",
    "list_stalled_requests",
    "list_unclaimed_requests",
    "mark_request_paid",
    "record_guest_reminder",
    "rename_category",
    "router",
    "staff_stats",
    "transfer_request",
]

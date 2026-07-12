"""CANONICAL: публичный интерфейс модуля requests (Task 0012, FOUNDATION §5.2, R-5).

Единственная точка входа в модуль снаружи: composition root (подключает
`router`, Task 0013), AI-инструменты (Task 0015) и другие доменные модули
импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны (контракт
import-linter). Здесь нет логики, только контракт: сервисные функции,
схемы границ, события, коды ошибок и HTTP-роутер.
"""

from __future__ import annotations

from hospitality.modules.requests.events import RequestCreated, RequestStatusChanged
from hospitality.modules.requests.models import RequestStatus
from hospitality.modules.requests.router import router
from hospitality.modules.requests.schemas import (
    RequestCategoryCreate,
    RequestCategoryRead,
    ServiceRequestCreate,
    ServiceRequestPage,
    ServiceRequestRead,
    ServiceRequestStatusUpdate,
)
from hospitality.modules.requests.service import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    STATUS_TRANSITIONS,
    change_request_status,
    create_category,
    create_request,
    get_request,
    list_categories,
    list_requests,
)

__all__ = [
    "ERR_REQUESTS_CATEGORY_KEY_TAKEN",
    "ERR_REQUESTS_CATEGORY_NOT_FOUND",
    "ERR_REQUESTS_INVALID_STATUS_TRANSITION",
    "ERR_REQUESTS_REQUEST_NOT_FOUND",
    "STATUS_TRANSITIONS",
    "RequestCategoryCreate",
    "RequestCategoryRead",
    "RequestCreated",
    "RequestStatus",
    "RequestStatusChanged",
    "ServiceRequestCreate",
    "ServiceRequestPage",
    "ServiceRequestRead",
    "ServiceRequestStatusUpdate",
    "change_request_status",
    "create_category",
    "create_request",
    "get_request",
    "list_categories",
    "list_requests",
    "router",
]

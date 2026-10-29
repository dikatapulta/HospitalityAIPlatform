"""Публичный интерфейс модуля documents (spec 0049 §5, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: канал гостя (форма, PR 4), кабинет
(страница «Документы для АВР», PR 3), подписчик разбора и фото моделью
(`ai/act_requisites.py`, PR 5) и джоба ретеншна импортируют ТОЛЬКО отсюда —
остальные файлы модуля приватны (контракт 5 import-linter). Здесь нет логики, только контракт.
"""

from __future__ import annotations

from hospitality.modules.documents.events import ActRequestSubmitted
from hospitality.modules.documents.export import (
    EXPORT_LIMIT,
    ExportOutcome,
    RequisitesExport,
    export_checked_requisites,
)
from hospitality.modules.documents.models import ExtractionState, FileTextState
from hospitality.modules.documents.processing import (
    ABANDONED_AFTER,
    anonymize_expired_act_requests,
    delete_abandoned_act_requests,
    extract_requisites,
)
from hospitality.modules.documents.scans import (
    ScanImage,
    apply_scan_requisites,
    clean_scan_value,
    scan_images,
)
from hospitality.modules.documents.schemas import (
    MAX_FILES,
    MAX_REQUESTS_PER_STAY,
    ActingUser,
    ActRequestFileRead,
    ActRequestRead,
    ActRequestRow,
    ActRequestSubmit,
    FieldWarning,
    RequisiteField,
    Requisites,
    RequisitesStatus,
    RequisitesUpdate,
    UploadInput,
)
from hospitality.modules.documents.service import (
    ERR_DOCUMENTS_DISABLED,
    ERR_DOCUMENTS_EMPTY,
    ERR_DOCUMENTS_FIELD_FORMAT,
    ERR_DOCUMENTS_NOT_FOUND,
    ERR_DOCUMENTS_STAY_LIMIT,
    ERR_DOCUMENTS_TOO_MANY_FILES,
    REQUEST_SUMMARY,
    ActRequestFileError,
    accepts_act_requests,
    get_act_request_by_request,
    list_act_requests,
    mark_requisites_checked,
    read_act_request_file,
    submit_act_request,
    update_requisites,
)

__all__ = [
    "ABANDONED_AFTER",
    "ERR_DOCUMENTS_DISABLED",
    "ERR_DOCUMENTS_EMPTY",
    "ERR_DOCUMENTS_FIELD_FORMAT",
    "ERR_DOCUMENTS_NOT_FOUND",
    "ERR_DOCUMENTS_STAY_LIMIT",
    "ERR_DOCUMENTS_TOO_MANY_FILES",
    "EXPORT_LIMIT",
    "MAX_FILES",
    "MAX_REQUESTS_PER_STAY",
    "REQUEST_SUMMARY",
    "ActRequestFileError",
    "ActRequestFileRead",
    "ActRequestRead",
    "ActRequestRow",
    "ActRequestSubmit",
    "ActRequestSubmitted",
    "ActingUser",
    "ExportOutcome",
    "ExtractionState",
    "FieldWarning",
    "FileTextState",
    "RequisiteField",
    "Requisites",
    "RequisitesExport",
    "RequisitesStatus",
    "RequisitesUpdate",
    "ScanImage",
    "UploadInput",
    "accepts_act_requests",
    "anonymize_expired_act_requests",
    "apply_scan_requisites",
    "clean_scan_value",
    "delete_abandoned_act_requests",
    "export_checked_requisites",
    "extract_requisites",
    "get_act_request_by_request",
    "list_act_requests",
    "mark_requisites_checked",
    "read_act_request_file",
    "scan_images",
    "submit_act_request",
    "update_requisites",
]

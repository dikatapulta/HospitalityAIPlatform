"""Страница «Документы для АВР» (spec 0049 §5–§6, Р-5, Р-10; issue #493, PR 3 серии).

Реквизиты, которые гость прислал скрепкой в чате, строкой таблицы: поля,
подсказки, файлы, «Проверить» / «Проверено» и шаги заявки («Взять», «Готово»,
«Вернуть гостю» — те же JSON-действия, что у очереди). Данные — модуль
`documents` (поля, правка, «Проверено», файлы) и `requests` (статус и `#N`);
здесь — права, фильтры и вид строки.

Право — одна функция `can_handle_act_requests` на меню, страницу, JSON-действия
и выдачу файла (P-12): служба «Бухгалтерия», ресепшен и менеджер (Р-10).
Пункт меню — ещё и при `ENABLE_GUEST_DOCUMENTS` (Р-7): там, где приём выключен,
пункта нет, а страница по ссылке из очереди открывает прошлые запросы до их срока.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from typing import Any, Final

from hospitality.modules.documents import api as documents_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import ERR_FILE_KEY
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import ERR_AUTH_FORBIDDEN, StaffContext
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import changes, checkin

logger = get_logger(module=__name__)

RequisiteField = documents_api.RequisiteField
RequestStatus = requests_api.RequestStatus

# Потолок строк страницы: строка живёт и после обезличивания (spec 0032), и
# длинный период без потолка читал бы все. Период по умолчанию — 30 дней.
PAGE_LIMIT: Final = 300
_DEFAULT_DAYS: Final = 30

# Столбцы полей в порядке карточки контрагента 1С (spec 0049 §6, слово основателя
# 28.10): бухгалтер переносит строку в 1С сверху вниз — контрагент, БИН/ИИН,
# адрес, затем банковский счёт. «Контрагент» — это `company_name`.
FIELD_LABELS: Final[dict[RequisiteField, str]] = {
    RequisiteField.COMPANY_NAME: "Контрагент",
    RequisiteField.BIN_IIN: "БИН/ИИН",
    RequisiteField.ADDRESS: "Адрес",
    RequisiteField.IIK: "ИИК",
    RequisiteField.BIK: "БИК",
    RequisiteField.BANK_NAME: "Банк",
}
# Коды — одной строкой в таблице: разорванный ИИК не перепечатать в 1С.
_CODE_FIELDS: Final = frozenset({RequisiteField.BIN_IIN, RequisiteField.IIK, RequisiteField.BIK})
_WARNING_TEXTS: Final = {
    documents_api.FieldWarning.CHECKSUM: "Контрольная цифра не сходится — проверьте",
    documents_api.FieldWarning.AMBIGUOUS: "Несколько вариантов — выберите сами",
}
_FILE_HINTS: Final = {
    documents_api.FileTextState.PENDING: "Разбирается",
    documents_api.FileTextState.NO_TEXT: "Скан — поля заполните сами",
    documents_api.FileTextState.IMAGE: "Скан — поля заполните сами",
    documents_api.FileTextState.FAILED: "Не разобран",
}
# Статус заявки запроса — «запрос», мужской род.
_REQUEST_STATUS_LABELS: Final = {
    RequestStatus.NEW: "новый",
    RequestStatus.IN_PROGRESS: "в работе",
    RequestStatus.DONE: "готово",
    RequestStatus.CANCELLED: "возвращён гостю",
}
# Язык гостя, если не русский: примечание бухгалтера уходит как написано (Р-2).
_LANGUAGE_NAMES: Final = {"en": "English", "kk": "Қазақша"}

UNAVAILABLE_TEXT: Final = "Реквизиты сейчас недоступны — сообщите администратору (ERR-FILES-005)."
EMPTY_TEXT: Final = "Запросов пока нет. Гости присылают реквизиты скрепкой в чате."


def can_handle_act_requests(staff: StaffContext) -> bool:
    """Служба «Бухгалтерия» любого уровня, ресепшен и менеджер (Р-10, docs/RBAC.md §2)."""
    return (
        staff.role_key is StaffRole.MANAGER
        or staff.is_reception
        or ACCOUNTING_SERVICE_KEY in staff.service_keys
    )


def nav_visible(staff: StaffContext) -> bool:
    """Пункт меню: право и приём реквизитов на контуре (Р-7, ревью PR #500 Н-5).

    Без выключателя пункта нет: на контуре без ключа файлов страница со
    строками ответила бы ERR-FILES-005, а строк там и не появится.
    """
    return get_settings().enable_guest_documents and can_handle_act_requests(staff)


def check_right(staff: StaffContext) -> bool:
    """Страница и выдача файла: право или отказ с логом (страница рисует «нет доступа»)."""
    if can_handle_act_requests(staff):
        return True
    logger.warning("staff.act_requests_denied", user_id=str(staff.user_id))
    return False


def require_right(staff: StaffContext) -> None:
    """JSON-действия: нет права — 403 ERR-AUTH-003, как у `require_role`."""
    if not check_right(staff):
        raise AppError(
            code=ERR_AUTH_FORBIDDEN,
            message="Act requests are not available to you",
            status_code=403,
        )


def page_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/act-requests"


def is_act_request(request: requests_api.ServiceRequestRead, category_key: str | None) -> bool:
    """Заявка — запрос АВР: форма гостя в службе «Бухгалтерия» (Р-1).

    Карточка очереди ведёт такую заявку на страницу (§6). Переданная в другую
    службу ссылку теряет — данные запроса при этом на странице остаются.
    """
    return (
        request.origin is requests_api.ServiceRequestOrigin.GUEST_FORM
        and category_key == ACCOUNTING_SERVICE_KEY
    )


@dataclass(frozen=True)
class Filters:
    """Период по дням отеля (включительно) и отметка строки."""

    day_from: date
    day_to: date
    status: documents_api.RequisitesStatus | None


def parse_filters(query: dict[str, str], *, today: date) -> Filters:
    """Фильтры из query; непонятное — умолчание, не 422 (канон `changes.parse_filters`)."""
    day_to = _parse_day(query.get("to"), today)
    day_from = _parse_day(query.get("from"), day_to - timedelta(days=_DEFAULT_DAYS - 1))
    if day_from > day_to:
        day_from = day_to
    try:
        status: documents_api.RequisitesStatus | None = documents_api.RequisitesStatus(
            query.get("status", "")
        )
    except ValueError:
        status = None
    return Filters(day_from=day_from, day_to=day_to, status=status)


def _parse_day(raw: str | None, default: date) -> date:
    try:
        day = date.fromisoformat(raw or "")
    except ValueError:
        return default
    return day if 2000 <= day.year <= 2100 else default


async def build_act_requests_context(staff: StaffContext, query: dict[str, str]) -> dict[str, Any]:
    """Контекст `act_requests.html`: фильтры, строки, тексты пустой и сбойной страницы.

    Строка без заявки (брошенный запрос — сбой между строкой и заявкой, ревью
    PR #510 Н-28) не показывается: «Готово» по ней дало бы ERR-REQUESTS-002,
    а гость получил «Не получилось отправить» и пришлёт ещё раз.
    """
    zone = await checkin.hotel_zone(staff)
    today = utc_now().astimezone(zone).date()
    filters = parse_filters(query, today=today)
    created_from, _ = changes.day_window_utc(filters.day_from, zone)
    _, created_before = changes.day_window_utc(filters.day_to, zone)
    context: dict[str, Any] = {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "from_value": filters.day_from.isoformat(),
        "to_value": filters.day_to.isoformat(),
        "status_value": filters.status.value if filters.status is not None else "",
        "field_labels": list(FIELD_LABELS.values()),
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api",
        "rows": [],
        "truncated_text": None,
        "empty_text": EMPTY_TEXT,
        "unavailable_text": None,
    }
    try:
        rows = await documents_api.list_act_requests(
            created_from=created_from,
            created_before=created_before,
            status=filters.status,
            limit=PAGE_LIMIT + 1,
        )
    except AppError as error:
        if error.code != ERR_FILE_KEY:
            raise
        context["unavailable_text"] = UNAVAILABLE_TEXT
        return context
    requests = {
        request.id: request
        for request in await requests_api.list_requests_by_ids([row.request_id for row in rows])
    }
    context["rows"] = [
        _row(staff.tenant_slug, row, requests[row.request_id], zone, today)
        for row in rows[:PAGE_LIMIT]
        if row.request_id in requests
    ]
    if len(rows) > PAGE_LIMIT:
        context["truncated_text"] = f"Показаны {PAGE_LIMIT} самых новых — сузьте даты."
    return context


def _row(
    tenant_slug: str,
    row: documents_api.ActRequestRow,
    request: requests_api.ServiceRequestRead,
    zone: tzinfo,
    today: date,
) -> dict[str, Any]:
    editable = not row.anonymized
    checked = row.status is documents_api.RequisitesStatus.CHECKED
    return {
        "id": str(row.id),
        "request_id": str(request.id),
        "room_number": row.room_number,
        "number": _number(request, today),
        "dates": (
            f"{row.check_in_at.astimezone(zone):%d.%m}–{row.check_out_at.astimezone(zone):%d.%m}"
        ),
        "fields": [
            {
                "name": field.value,
                "label": label,
                "value": getattr(row.requisites, field.value),
                "code": field in _CODE_FIELDS,
                "warning": _WARNING_TEXTS.get(row.warnings[field])
                if field in row.warnings
                else None,
            }
            for field, label in FIELD_LABELS.items()
        ],
        "files": [_file(tenant_slug, row.id, file) for file in row.files],
        "guest_wrote": " — ".join(text for text in (row.bill_to, row.note) if text),
        "language_name": _LANGUAGE_NAMES.get(row.guest_language),
        "anonymized": row.anonymized,
        "checked": checked,
        "checked_line": _checked_line(row, zone) if checked else None,
        "request_status": _REQUEST_STATUS_LABELS[request.status],
        "can_edit": editable,
        "can_check": editable and not checked,
        "can_claim": request.status is RequestStatus.NEW,
        "can_complete": request.status is RequestStatus.IN_PROGRESS,
        "can_return": request.status in (RequestStatus.NEW, RequestStatus.IN_PROGRESS),
    }


def _number(request: requests_api.ServiceRequestRead, today: date) -> str:
    """«#12», а заявка не сегодняшнего дня — «#12 · 25.10»: номер уникален внутри дня."""
    number = f"#{request.daily_number}" if request.daily_number else "#?"
    if request.service_day is not None and request.service_day != today:
        number += f" · {request.service_day:%d.%m}"
    return number


def _file(
    tenant_slug: str, act_request_id: uuid.UUID, file: documents_api.ActRequestFileRead
) -> dict[str, str | None]:
    return {
        "href": f"{page_path(tenant_slug)}/{act_request_id}/files/{file.file_id}",
        "name": file.original_name,
        "hint": _FILE_HINTS.get(file.text_state),
    }


def _checked_line(row: documents_api.ActRequestRow, zone: tzinfo) -> str:
    """«Алия · 28.10 14:05» — кто и когда сверил (имя — снапшот, может быть стёрто)."""
    moment = f"{row.checked_at.astimezone(zone):%d.%m %H:%M}" if row.checked_at else ""
    return " · ".join(part for part in (row.checked_by_display_name, moment) if part)

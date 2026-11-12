"""Черновик АВР на странице «Документы для АВР» (spec 0086 §4, Р-9; issue #953, PR 2 серии).

У строки «Проверено» — «Сформировать АВР» и форма, под строкой — её акты со
ссылками на файл. Снимок, суммы, журнал и файл — модуль `documents`
(`create/list/render_work_completion_act`); здесь — выключатель контура,
предзаполнение формы из проживания и вид строки списка. Право — то же, что у
страницы (`act_requests.can_handle_act_requests`, Р-9), его проверяют маршруты.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, tzinfo
from decimal import Decimal
from typing import Any, Final

from fastapi import Response

from hospitality.modules.documents import api as documents_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import attachment_disposition
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.ports.accounting import ActFileFormat

_NBSP: Final = " "
# Первая строка формы — проживание из строки запроса (§4); цену вписывает бухгалтер.
STAY_UNIT: Final = "сутки"
# Ссылки на файл под строкой акта; формат без подписи здесь — KeyError в тестах.
_FILE_LABELS: Final[dict[ActFileFormat, str]] = {ActFileFormat.XLSX: "Excel"}


def section_enabled() -> bool:
    """`ENABLE_WORK_COMPLETION_ACTS` (Р-9): одна проверка на кнопку, список и маршруты."""
    return get_settings().enable_work_completion_acts


def file_path(tenant_slug: str, act_id: uuid.UUID, file_format: ActFileFormat) -> str:
    return f"/staff/{tenant_slug}/act-requests/acts/{act_id}.{file_format.value}"


def can_make_act(
    row: documents_api.ActRequestRow, request: requests_api.ServiceRequestRead
) -> bool:
    """«Проверено», не обезличена, не возвращена гостю (Р-2).

    Тот же отбор повторяет `documents.create_work_completion_act` (ERR-DOCUMENTS-007):
    кнопка лишь не предлагает заведомый отказ.
    """
    return (
        row.status is documents_api.RequisitesStatus.CHECKED
        and not row.anonymized
        and request.status is not requests_api.RequestStatus.CANCELLED
    )


async def acts_of_rows(
    rows: Sequence[documents_api.ActRequestRow],
) -> dict[uuid.UUID, list[documents_api.WorkCompletionActRead]]:
    """Акты строк страницы; раздел выключен — ни одного запроса к таблице актов."""
    if not section_enabled():
        return {}
    return await documents_api.list_work_completion_acts([row.id for row in rows])


def row_extras(
    tenant_slug: str,
    row: documents_api.ActRequestRow,
    request: requests_api.ServiceRequestRead,
    acts: Sequence[documents_api.WorkCompletionActRead],
    zone: tzinfo,
    today: date,
) -> dict[str, Any]:
    """Поля строки шаблона: `act_form` (None — кнопки нет) и `acts` — строки списка."""
    if not section_enabled():
        return {"act_form": None, "acts": []}
    return {
        "act_form": _form(row, zone, today) if can_make_act(row, request) else None,
        "acts": [_act_line(tenant_slug, act) for act in acts],
    }


async def file_response(
    staff: StaffContext, act_id: uuid.UUID, file_format: ActFileFormat
) -> Response:
    """Файл акта вложением, без кэша (§3); чужой или обезличенный акт — 404 ERR-DOCUMENTS-004."""
    rendered = await documents_api.render_work_completion_act(
        act_id, file_format, acting_user=staff.actor()
    )
    return Response(
        content=rendered.content,
        media_type=rendered.content_type,
        headers={
            "Content-Disposition": attachment_disposition(rendered.filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _form(row: documents_api.ActRequestRow, zone: tzinfo, today: date) -> dict[str, str]:
    """Заголовок и первая строка: «Проживание, номер 512, 10.11–12.11.2026», сутки, ночи."""
    check_in = row.check_in_at.astimezone(zone).date()
    check_out = row.check_out_at.astimezone(zone).date()
    company = row.requisites.company_name
    room = f"номер {row.room_number}"
    return {
        "title": f"АВР для {company} · {room}" if company else f"АВР · {room}",
        "composed_on": today.isoformat(),
        "service": f"Проживание, {room}, {stay_period(check_in, check_out)}",
        "unit": STAY_UNIT,
        # Выезд в день заезда — одни сутки, а не ноль: количество в акте > 0 (Р-3).
        "quantity": str(max((check_out - check_in).days, 1)),
    }


def stay_period(start: date, end: date) -> str:
    """«10.11–12.11.2026»; через Новый год — обе даты с годом."""
    if start.year == end.year:
        return f"{start:%d.%m}–{end:%d.%m.%Y}"
    return f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


def _act_line(tenant_slug: str, act: documents_api.WorkCompletionActRead) -> dict[str, Any]:
    """«АВР № 15 от 11.11.2026 · 45 000,00 ₸ · Айгуль»; обезличенный — без имени и файлов."""
    parts = [f"АВР № {act.number} от {act.composed_on:%d.%m.%Y}", f"{money(act.total)}{_NBSP}₸"]
    if act.created_by_display_name:
        parts.append(act.created_by_display_name)
    files = (
        []
        if act.anonymized
        else [
            {
                "label": _FILE_LABELS[file_format],
                "href": file_path(tenant_slug, act.id, file_format),
            }
            for file_format in ActFileFormat
        ]
    )
    return {"text": " · ".join(parts), "files": files}


def money(value: Decimal) -> str:
    """«45 000,00» — неразрывные пробелы и запятая, как в «Итого» формы (§4)."""
    return f"{value:,.2f}".replace(",", _NBSP).replace(".", ",")

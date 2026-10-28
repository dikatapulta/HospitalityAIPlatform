"""Снапшот заявки запроса для журнала изменений (spec 0049 Р-10, spec 0041 §2.1).

«Что» строки журнала — заявка запроса: `#12 · Бухгалтерия · 512`. Значений полей
в журнале нет по построению — у `StaffActionRecord` нет таких полей. Строку
добавляет `service.py` той же сессией, что правку или «Проверено».
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.modules.documents.models import ActRequest
from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_actions import StaffAction, StaffActionRecord
from hospitality.shared.db import session_scope


async def journal_record(
    act_request_id: uuid.UUID, action: StaffAction
) -> StaffActionRecord | None:
    """Снапшот `#N`, его дня, службы и комнаты заявки этого запроса.

    Читается до блокировки строки запроса — своими сессиями, а не внутри
    транзакции правки. Запроса или его заявки нет (брошенный запрос) — None:
    сервис отвечает ERR-DOCUMENTS-004, страница таких строк не показывает.
    """
    async with session_scope() as session:
        request_id = await session.scalar(
            select(ActRequest.request_id).where(ActRequest.id == act_request_id)
        )
    requests = await requests_api.list_requests_by_ids([request_id]) if request_id else []
    if not requests:
        return None
    request = requests[0]
    names = {category.id: category.name for category in await requests_api.list_categories()}
    return StaffActionRecord(
        action=action,
        request_id=request.id,
        request_daily_number=request.daily_number,
        request_service_day=request.service_day,
        category_name=names.get(request.category_id),
        category_id=request.category_id,
        room_number=request.room_number,
    )

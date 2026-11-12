"""JSON-действия страницы «Документы для АВР» (spec 0049 §5, Р-5, Р-10; issue #493).

Файл по образцу `turnover_api.py`, канон тот же: CSRF-щит `browser.require_json_same_origin`,
личность — `staff_auth.require_role`, право — `act_requests.require_right`
(служба «Бухгалтерия», ресепшен, менеджер), исходы — конверт ошибок R-8.
Формат поля, «правил человек» и журнал — модуль `documents`. «Взять», «Готово»
и «Вернуть гостю» — действия очереди (`queue_api.py`): у запроса та же заявка.
Действия зовёт `static/act_requests.js`, «Сформировать АВР» (spec 0086) —
`static/work_completion_acts.js`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, HTTPException, Request

from hospitality.modules.documents import api as documents_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import act_requests, work_completion_acts
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Уровень — любой; право — служба и уровень, `act_requests.can_handle_act_requests`.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)


async def _actor(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    staff = await _any_role(request)
    act_requests.require_right(staff)
    return staff


@router.post("/act-requests/{act_request_id}/requisites", summary="Исправить поля реквизитов")
async def update_requisites(
    request: Request, act_request_id: uuid.UUID, body: documents_api.RequisitesUpdate
) -> dict[str, bool]:
    """Только переданные поля; "" — стереть. Формат — ERR-DOCUMENTS-006 (422),
    правка снимает «Проверено» (Р-5). Значения полей в лог не пишутся."""
    staff = await _actor(request)
    await documents_api.update_requisites(act_request_id, body, acting_user=staff.actor())
    return {"ok": True}


@router.post("/act-requests/{act_request_id}/checked", summary="Реквизиты проверены")
async def mark_checked(request: Request, act_request_id: uuid.UUID) -> dict[str, bool]:
    """«Проверено» (Р-5): человек сверил значения строки."""
    staff = await _actor(request)
    await documents_api.mark_requisites_checked(act_request_id, acting_user=staff.actor())
    return {"ok": True}


@router.post("/act-requests/{act_request_id}/acts", summary="Сформировать черновик АВР")
async def create_act(
    request: Request, act_request_id: uuid.UUID, body: documents_api.WorkCompletionActCreate
) -> dict[str, str]:
    """Черновик АВР из проверенных реквизитов (spec 0086 Р-2, Р-3): только по этой
    кнопке бухгалтера (NG-4). Не «Проверено», возвращён гостю — 409 ERR-DOCUMENTS-007;
    у отеля нет исполнителя — 409 008; формат полей — 422 схемы. Раздел выключен — 404."""
    if not work_completion_acts.section_enabled():
        raise HTTPException(status_code=404)
    staff = await _actor(request)
    act = await documents_api.create_work_completion_act(
        act_request_id, body, acting_user=staff.actor()
    )
    return {"id": str(act.id)}

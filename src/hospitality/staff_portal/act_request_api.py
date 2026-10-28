"""JSON-действия страницы «Документы для АВР» (spec 0049 §5, Р-5, Р-10; issue #493).

Отдельный файл по образцу `turnover_api.py` (R-3: `api_router.py` и так за
границей размера). Канон тот же: CSRF-щит `browser.require_json_same_origin`,
личность — `staff_auth.require_role`, право — `act_requests.require_right`
(служба «Бухгалтерия», ресепшен, менеджер), исходы — конверт ошибок R-8.
Формат поля, «правил человек» и журнал — модуль `documents`. «Взять», «Готово»
и «Вернуть гостю» — действия очереди (`api_router.py`): у запроса та же заявка.
Действия зовёт `static/act_requests.js`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request

from hospitality.modules.documents import api as documents_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import act_requests
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

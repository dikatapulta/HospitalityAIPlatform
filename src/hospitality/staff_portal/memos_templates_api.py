"""«Шаблоны записок»: `POST …/api/memos/templates` — список целиком (spec 0077 §7, §8; PR 6).

Канон `knowledge_api.py`: CSRF-щит `require_json_same_origin` и `require_role(manager)`
напрямую; раздел выключен у отеля — 404. Правка — `memos_templates.save_templates` под
блокировкой строки тенанта. Пустое название или текст, больше 10 шаблонов, длиннее
пределов — 422 ERR-PLATFORM-002 (экран не даёт отправить такое и сам называет причину).
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request

from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import memos, memos_templates
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api/memos", tags=["staff-portal"])

_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


@router.post("/templates", summary="«Шаблоны записок»: сохранить список (spec 0077 §7)")
async def save_templates(
    request: Request, body: memos_templates.TemplatesInput
) -> dict[str, list[str]]:
    require_json_same_origin(request)
    staff = await _manager_role(request)
    await memos.require_section(staff)
    return {"keys": await memos_templates.save_templates(staff, body.templates)}

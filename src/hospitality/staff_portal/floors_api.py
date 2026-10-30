"""JSON-действие страницы «Этажи» (spec 0053 §5, issue #478): «Сохранить раздачу».

Отдельный файл, а не `api_router.py` (R-3: тот и так за границей размера).
Канон `turnover_api.py`: раздел выключен — 404 до CSRF и личности, затем
CSRF-щит `browser.require_json_same_origin` и `staff_auth.require_role`.
Поле этажей разбирает схема (`floors.parse_floors`): непонятое — обычная ошибка
проверки запроса с номером строки в `loc`, по нему `static/floors.js` называет
горничную. Право раздавать и состав службы проверяет `floors.save_floors`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import floors
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Уровень — любой; право раздавать — `floors.rights_for`.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)

# Строк — не больше, чем людей в большой службе уборки, с запасом.
_MAX_ROWS: Final = 500


class FloorsRowBody(BaseModel):
    """Строка формы: сотрудник уборки и его этажи текстом «7, 8, 12-14»."""

    user_id: uuid.UUID
    floors: frozenset[int]

    @field_validator("floors", mode="before")
    @classmethod
    def _parse(cls, value: object) -> frozenset[int]:
        if not isinstance(value, str) or len(value) > floors.FLOORS_TEXT_MAX_LENGTH:
            raise ValueError("floors must be text up to 200 characters")
        return floors.parse_floors(value)


class FloorsBody(BaseModel):
    """Раздача целиком: сотрудника нет в списке — у него этажей нет."""

    rows: list[FloorsRowBody] = Field(max_length=_MAX_ROWS)


class FloorsSavedView(BaseModel):
    members: int


@router.post("/floors", summary="Сохранить раздачу этажей хаускипинга")
async def save_floors(request: Request, body: FloorsBody) -> FloorsSavedView:
    """Старший уборки и менеджер (spec 0053 §5): не по праву — 403
    ERR-HOUSEKEEPING-003, в раздаче не сотрудник уборки — 409 006."""
    if not floors.section_enabled():
        raise HTTPException(status_code=404)
    require_json_same_origin(request)
    staff = await _any_role(request)
    floors_by_user: dict[uuid.UUID, frozenset[int]] = {}
    for row in body.rows:
        floors_by_user[row.user_id] = floors_by_user.get(row.user_id, frozenset()) | row.floors
    await floors.save_floors(staff, floors_by_user)
    return FloorsSavedView(members=len(floors_by_user))

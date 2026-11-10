"""Действия «Назначить экзамен» и «Снять» на «Итогах экзаменов» (spec 0072 §3; #898).

Канон `mentor_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`mentor.manager_role` (только менеджер, docs/RBAC.md), раздел выключен у отеля — 404.
Службы сверяются с категориями отеля тем же правилом, что галочки «Сотрудников»
(`team.normalized_service_keys`, ERR-AUTH-015). Скрипт страницы — `static/mentor_results.js`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal import mentor
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.mentor_api import MAX_CHAPTER_NUMBER
from hospitality.staff_portal.team import normalized_service_keys

router = APIRouter(prefix="/{tenant_slug}/api/mentor/exam/assignments", tags=["staff-portal"])

# Глав у «Наставника» меньше сотни, служб у отеля — десяток; пределы — с запасом.
MAX_ASSIGN_CHAPTERS: Final = 500
MAX_ASSIGN_SERVICES: Final = 100

ChapterNumber = Annotated[int, Field(ge=1, le=MAX_CHAPTER_NUMBER)]
ServiceKey = Annotated[str, Field(min_length=1, max_length=64)]


class AssignBody(BaseModel):
    """`chapters=None` — весь кодекс, `service_keys=None` — всем; пустой список — 422
    (форма шлёт `None`, а «отметьте хотя бы одну» проверяет до отправки)."""

    model_config = ConfigDict(extra="forbid")

    chapters: list[ChapterNumber] | None = Field(
        default=None, min_length=1, max_length=MAX_ASSIGN_CHAPTERS
    )
    service_keys: list[ServiceKey] | None = Field(
        default=None, min_length=1, max_length=MAX_ASSIGN_SERVICES
    )


class AssignedView(BaseModel):
    id: uuid.UUID


async def _manager(request: Request) -> StaffContext:
    """CSRF и личность менеджера — до всего; затем раздел у отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await mentor.manager_role(request)
    await mentor.require_section(staff)
    return staff


@router.post("")
async def assign_exam(request: Request, body: AssignBody) -> AssignedView:
    """«Назначить»: служба не из категорий отеля — 422 ERR-AUTH-015; ни одной главы нет
    в нынешнем кодексе — 409 ERR-MENTOR-006; кодекса нет — 409 ERR-MENTOR-002."""
    staff = await _manager(request)
    service_keys = (
        []
        if body.service_keys is None
        else await normalized_service_keys(StaffRole.STAFF, body.service_keys)
    )
    assignment = await mentor_api.create_exam_assignment(
        chapter_numbers=body.chapters,
        service_keys=service_keys,
        created_by_user_id=staff.user_id,
    )
    return AssignedView(id=assignment.id)


@router.post("/{assignment_id}/archive")
async def archive_exam(request: Request, assignment_id: uuid.UUID) -> dict[str, bool]:
    """«Снять»: повтор и чужое назначение — `archived: false`, ничего не меняется."""
    await _manager(request)
    return {"archived": await mentor_api.archive_exam_assignment(assignment_id)}

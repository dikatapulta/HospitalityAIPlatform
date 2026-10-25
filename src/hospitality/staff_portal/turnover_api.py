"""JSON-действия доски «Выезды и уборка» (spec 0046 §4–§5, issue #476).

Отдельный файл, а не `api_router.py` (R-3: тот и так за границей размера).
Канон тот же: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role`, исходы — конверт ошибок R-8. Право на шаг режет
`turnovers.require_step_right` (ERR-HOUSEKEEPING-003), порядок шагов — домен
`housekeeping` под блокировкой строки (ERR-HOUSEKEEPING-002). Действия зовёт
`static/turnovers.js`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import checkin, turnovers
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Уровень — любой; право на шаг — служба и уровень, `turnovers.rights_for`.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)

TurnoverStatus = housekeeping_api.TurnoverStatus


class ReportBody(BaseModel):
    """«Отметить выезд»: номер комнаты; `check_out_stay` — подтверждение выселения."""

    room_number: str = Field(min_length=1, max_length=20)
    check_out_stay: bool = False

    @field_validator("room_number")
    @classmethod
    def _strip_room(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("room number must not be blank")
        return stripped


class InspectBody(BaseModel):
    """«Проверено» без тела или «Замечание» с текстом."""

    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ReportView(BaseModel):
    """Итог отметки: `created=False` — номер уже был у хаускипинга с `since`."""

    room_number: str
    created: bool
    since: str


class StepView(BaseModel):
    """Итог шага — страница перечитывает доску сама."""

    room_number: str
    status: TurnoverStatus


async def _actor(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await _any_role(request)


@router.post("/turnovers", summary="Отметить выезд по номеру комнаты")
async def report_checkout(request: Request, body: ReportBody) -> ReportView:
    """Ресепшен и менеджер (spec 0046 §5): гость в кабинете — 409
    ERR-HOUSEKEEPING-004 до подтверждения; хаускипинга нет — 409 005."""
    staff = await _actor(request)
    if not turnovers.rights_for(staff).can_report:
        raise turnovers.forbidden()
    report = await turnovers.report_checkout(
        staff, body.room_number, check_out_stay=body.check_out_stay
    )
    logger.info(
        "staff.turnover_reported",
        turnover_id=str(report.turnover.id),
        created=report.created,
        user_id=str(staff.user_id),
    )
    zone = await checkin.hotel_zone(staff)
    return ReportView(
        room_number=report.turnover.room_number,
        created=report.created,
        since=turnovers.clock(report.turnover.reported_at, zone, utc_now()),
    )


async def _step(
    request: Request, turnover_id: uuid.UUID, status: TurnoverStatus, note: str | None = None
) -> StepView:
    staff = await _actor(request)
    turnovers.require_step_right(staff, status)
    updated = await housekeeping_api.change_turnover_status(
        turnover_id, status, acting_user=staff.actor(), inspection_note=note
    )
    logger.info(
        "staff.turnover_step",
        turnover_id=str(turnover_id),
        status=status.value,
        with_note=note is not None,
        user_id=str(staff.user_id),
    )
    return StepView(room_number=updated.room_number, status=updated.status)


@router.post("/turnovers/{turnover_id}/inspect", summary="Номер проверен (с замечанием или без)")
async def inspect(request: Request, turnover_id: uuid.UUID, body: InspectBody) -> StepView:
    return await _step(request, turnover_id, TurnoverStatus.INSPECTED, body.note)


@router.post("/turnovers/{turnover_id}/start-cleaning", summary="Начать уборку")
async def start_cleaning(request: Request, turnover_id: uuid.UUID) -> StepView:
    return await _step(request, turnover_id, TurnoverStatus.CLEANING)


@router.post("/turnovers/{turnover_id}/finish-cleaning", summary="Номер убран")
async def finish_cleaning(request: Request, turnover_id: uuid.UUID) -> StepView:
    return await _step(request, turnover_id, TurnoverStatus.CLEANED)


@router.post("/turnovers/{turnover_id}/accept", summary="Принять номер — готов к заселению")
async def accept(request: Request, turnover_id: uuid.UUID) -> StepView:
    return await _step(request, turnover_id, TurnoverStatus.READY)


@router.post("/turnovers/{turnover_id}/cancel", summary="Отменить ошибочный выезд")
async def cancel(request: Request, turnover_id: uuid.UUID) -> StepView:
    return await _step(request, turnover_id, TurnoverStatus.CANCELLED)

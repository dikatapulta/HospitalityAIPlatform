"""JSON-действие страницы «Тестовые данные» (issue #740): «Заполнить тестовыми данными».

Отдельный файл по канону `floors_api.py`: контур и отель — 404 до CSRF и
личности (`demo_data.contour_allows`), затем CSRF-щит
`browser.require_json_same_origin` и `staff_auth.require_role` менеджера.
Построение — `tools/demo_showcase.build_showcase` с `--reset` и без приглашений;
два нажатия разом ждут друг друга там же. Отказ самой картины (нет менеджера,
служб, тенанта — то, что команда пишет в терминал) — 409 `ERR-PLATFORM-010`,
причина — в логе `staff.demo_data_failed`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import demo_data
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.home import plural
from hospitality.tools.demo_showcase import ShowcaseReport, build_showcase
from hospitality.tools.demo_showcase_staff import DemoShowcaseError

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)

ERR_DEMO_DATA_NOT_FILLED: Final = "ERR-PLATFORM-010"


def _duration_ru(delay: timedelta) -> str:
    """«10 минут», «3 часа», «1 час 30 минут» — срок до просрочки словами."""
    minutes = max(int(delay.total_seconds()) // 60, 0)
    hours, rest = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if rest or not hours:
        parts.append(f"{rest} {plural(rest, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts)


def done_message(report: ShowcaseReport) -> str:
    """«Готово: 13 гостей, 11 заявок, 3 брони столика. Одна заявка станет просроченной
    через 10 минут, другие — через 3 часа.» — числа из итога построения; брони
    выключены у отеля — без них."""
    stays = plural(report.stays, "гость", "гостя", "гостей")
    requests = plural(report.requests, "заявка", "заявки", "заявок")
    message = f"Готово: {report.stays} {stays}, {report.requests} {requests}"
    if report.bookings:
        bookings = plural(report.bookings, "бронь", "брони", "броней")
        message += f", {report.bookings} {bookings} столика"
    message += "."
    delays = [_duration_ru(delay) for delay in report.overdue_after]
    if len(delays) == 1:
        message += f" Одна заявка станет просроченной через {delays[0]}."
    elif len(delays) > 1:
        others = "другая" if len(delays) == 2 else "другие"
        message += (
            f" Одна заявка станет просроченной через {delays[0]}, {others} — через {delays[-1]}."
        )
    return message


class DemoDataFilledView(BaseModel):
    """Итог нажатия: текст для страницы и числа построенной картины."""

    message: str
    stays: int
    requests: int
    bookings: int


@router.post(
    "/demo-data",
    summary="Заполнить демо-отель тестовыми данными (только staging, issue #740)",
    include_in_schema=False,
)
async def fill_demo_data(request: Request, tenant_slug: str) -> DemoDataFilledView:
    """Порядок: контур и отель (404) → CSRF-щит → менеджер (403) → построение."""
    if not demo_data.contour_allows(tenant_slug):
        raise HTTPException(status_code=404)
    require_json_same_origin(request)
    staff = await _manager_role(request)
    try:
        report = await build_showcase(confirmed=True, reset=True, invite=False)
    except DemoShowcaseError as error:
        logger.warning(
            "staff.demo_data_failed",
            error_code=ERR_DEMO_DATA_NOT_FILLED,
            user_id=str(staff.user_id),
            reason=str(error),
        )
        raise AppError(
            code=ERR_DEMO_DATA_NOT_FILLED,
            message="Demo data was not filled; the reason is in the server log",
            status_code=409,
        ) from error
    logger.info("staff.demo_data_filled", user_id=str(staff.user_id))
    return DemoDataFilledView(
        message=done_message(report),
        stays=report.stays,
        requests=report.requests,
        bookings=report.bookings,
    )

"""Доска «Выезды и уборка» (spec 0046 §4–§6, issue #476): права, данные и отметка выезда.

Ресепшен отмечает выезд по номеру комнаты, хаускипинг ведёт номер до «готов к
заселению», старший хаускипинга принимает. Сам путь номера — модуль
`housekeeping` (ADR-021); здесь то, что знает только кабинет: кто какой шаг
вправе нажать (`rights_for` — одна функция на меню, кнопки и JSON-действия,
P-12), выселение гостя из кабинета вместе с отметкой выезда и вид доски.
Статус номера в OPERA кабинет не трогает (NG-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.platform.staff_team import staffed_service_keys
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import queue

logger = get_logger(module=__name__)

TurnoverStatus = housekeeping_api.TurnoverStatus

# Коды каталога ошибок (docs/runbooks/errors.md, R-8); 001 и 002 — у домена.
ERR_HOUSEKEEPING_STEP_FORBIDDEN: Final = "ERR-HOUSEKEEPING-003"
ERR_HOUSEKEEPING_GUEST_IN_ROOM: Final = "ERR-HOUSEKEEPING-004"
ERR_HOUSEKEEPING_SERVICE_ABSENT: Final = "ERR-HOUSEKEEPING-005"

# Потолки доски (spec 0046 §6): открытых путей не больше, чем номеров у
# крупного отеля, готовых за день — с запасом на 350 номеров пилота. Открытых —
# и у сигнала кабинета (`signal.py`): одна граница скана на доску и сигнал.
OPEN_LIMIT: Final = 400
_READY_LIMIT: Final = 200

NO_HOUSEKEEPING_TEXT: Final = (
    "В кабинете пока нет сотрудников хаускипинга — выезд здесь никто не увидит. "
    "Сообщите им, как раньше. Менеджер добавит их на странице «Сотрудники»."
)


def board_enabled() -> bool:
    """`ENABLE_TURNOVER_BOARD` (spec 0046 §5): одна проверка на меню, страницу,
    действия и «Выселить» — выключено, и раздела нет совсем."""
    return get_settings().enable_turnover_board


@dataclass(frozen=True)
class TurnoverRights:
    """Что сотрудник вправе на доске (spec 0046 §4, docs/RBAC.md §2)."""

    can_view: bool
    can_report: bool
    can_work: bool
    can_accept: bool
    can_cancel: bool


def rights_for(staff: StaffContext) -> TurnoverRights:
    """Хаускипинг — шаги уборки; его старший — ещё приёмка и отмена; ресепшен —
    отметка выезда и отмена; менеджер — всё."""
    is_manager = staff.role_key is StaffRole.MANAGER
    housekeeping = staff.is_housekeeping
    housekeeping_senior = housekeeping and staff.role_key is StaffRole.SENIOR
    reception = staff.is_reception
    return TurnoverRights(
        can_view=is_manager or reception or housekeeping,
        can_report=is_manager or reception,
        can_work=is_manager or housekeeping,
        can_accept=is_manager or housekeeping_senior,
        can_cancel=is_manager or reception or housekeeping_senior,
    )


def require_step_right(staff: StaffContext, status: TurnoverStatus) -> None:
    """Шаг не по праву — ERR-HOUSEKEEPING-003 (403). Порядок шагов держит домен."""
    rights = rights_for(staff)
    allowed = {
        TurnoverStatus.INSPECTED: rights.can_work,
        TurnoverStatus.CLEANING: rights.can_work,
        TurnoverStatus.CLEANED: rights.can_work,
        TurnoverStatus.READY: rights.can_accept,
        TurnoverStatus.CANCELLED: rights.can_cancel,
    }.get(status, False)
    if not allowed:
        raise forbidden()


def forbidden() -> AppError:
    return AppError(
        code=ERR_HOUSEKEEPING_STEP_FORBIDDEN,
        message="This room turnover step is not available to you",
        status_code=403,
    )


async def housekeeping_in_cabinet(staff: StaffContext) -> bool:
    """У отеля есть служба уборки и в ней хотя бы один активный сотрудник (§5).

    Иначе путь номера никто не увидит, и отметка выезда только засоряла бы доску.
    """
    categories = await requests_api.list_categories()
    if not any(category.key == HOUSEKEEPING_SERVICE_KEY for category in categories):
        return False
    return HOUSEKEEPING_SERVICE_KEY in await staffed_service_keys(staff.tenant_id)


async def report_checkout(
    staff: StaffContext, room_number: str, *, check_out_stay: bool
) -> housekeeping_api.TurnoverReport:
    """«Отметить выезд» по номеру комнаты (spec 0046 §5).

    Гость этого номера заселён в кабинете — без `check_out_stay` отказ
    ERR-HOUSEKEEPING-004 (страница спросит подтверждение): одна опечатка в
    номере не должна молча выключать чат живому гостю. С подтверждением —
    выселение тем же `guests_api.check_out`, что у карточки, затем отметка.
    """
    if not await housekeeping_in_cabinet(staff):
        raise AppError(
            code=ERR_HOUSEKEEPING_SERVICE_ABSENT,
            message="No active housekeeping staff in the cabinet",
            status_code=409,
        )
    stay = await guests_api.find_active_stay(room_number)
    if stay is not None:
        if not check_out_stay:
            raise AppError(
                code=ERR_HOUSEKEEPING_GUEST_IN_ROOM,
                message="A guest is checked in to this room in the cabinet",
                status_code=409,
            )
        await guests_api.check_out(stay.id, acting_user=staff.actor())
        logger.info("staff.stay_checked_out", stay_id=str(stay.id), user_id=str(staff.user_id))
    return await housekeeping_api.report_checkout(room_number, acting_user=staff.actor())


async def report_after_stay_checkout(staff: StaffContext, room_number: str) -> str | None:
    """«Выселить» на карточке гостя отмечает выезд заодно; вернуть адрес доски.

    Доска выключена или хаускипинга в кабинете нет — None: гость выселен, а путь
    не создаётся (§5).
    """
    if not board_enabled() or not await housekeeping_in_cabinet(staff):
        return None
    report = await housekeeping_api.report_checkout(room_number, acting_user=staff.actor())
    logger.info(
        "staff.turnover_reported",
        turnover_id=str(report.turnover.id),
        created=report.created,
        user_id=str(staff.user_id),
    )
    return board_path(staff.tenant_slug)


def board_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/turnovers"


# Группы доски по статусу (spec 0046 §6) и подпись шага в строке номера.
_GROUPS: Final[tuple[tuple[TurnoverStatus, str], ...]] = (
    (TurnoverStatus.REPORTED, "Ждут проверки"),
    (TurnoverStatus.INSPECTED, "Проверены"),
    (TurnoverStatus.CLEANING, "Убираются"),
    (TurnoverStatus.CLEANED, "Ждут приёмки"),
)


async def build_turnovers_context(staff: StaffContext, *, with_form: bool) -> dict[str, Any]:
    """Доска целиком: группы открытых, готовые сегодня, «Сегодня убрано».

    `with_form` — страница с формой «Отметить выезд»: только ей нужно знать, есть
    ли хаускипинг в кабинете; фрагмент поллинга (каждые 15 с) эти запросы не делает.
    """
    rights = rights_for(staff)
    config = await queue.tenant_config(staff)
    zone: tzinfo = config.tzinfo if config is not None else UTC
    midnight = queue.hotel_midnight_utc(config)
    now = utc_now()
    open_turnovers = await housekeeping_api.list_open_turnovers(limit=OPEN_LIMIT)
    ready = await housekeeping_api.list_turnovers_ready_since(midnight, limit=_READY_LIMIT)
    groups = [
        {
            "title": title,
            "rows": [
                _row(turnover, rights, zone, now)
                for turnover in open_turnovers
                if turnover.status is status
            ],
        }
        for status, title in _GROUPS
    ]
    cleaned = await housekeeping_api.count_cleaned_since(midnight) if rights.can_accept else []
    return {
        "tenant_slug": staff.tenant_slug,
        "rights": rights,
        "housekeeping_ready": (
            await housekeeping_in_cabinet(staff) if with_form and rights.can_report else True
        ),
        "no_housekeeping_text": NO_HOUSEKEEPING_TEXT,
        "groups": [group for group in groups if group["rows"]],
        "ready_rows": [_row(turnover, rights, zone, now) for turnover in ready],
        "cleaned_line": _cleaned_line(cleaned) if rights.can_accept else None,
        "fragment_path": f"{board_path(staff.tenant_slug)}/fragment",
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/turnovers",
    }


def _row(
    turnover: housekeeping_api.RoomTurnoverRead,
    rights: TurnoverRights,
    zone: tzinfo,
    now: datetime,
) -> dict[str, Any]:
    status = turnover.status
    actions: list[tuple[str, str, str]] = []  # (действие, надпись, класс кнопки)
    if rights.can_work and status is TurnoverStatus.REPORTED:
        actions += [("inspect", "Проверено", "btn-primary"), ("remark", "Замечание", "btn-outline")]
    if rights.can_work and status is TurnoverStatus.INSPECTED:
        actions.append(("start-cleaning", "Убираю", "btn-primary"))
    if rights.can_work and status is TurnoverStatus.CLEANING:
        actions.append(("finish-cleaning", "Убрано", "btn-primary"))
    if rights.can_accept and status is TurnoverStatus.CLEANED:
        actions.append(("accept", "Принять", "btn-primary"))
    if rights.can_cancel and status in housekeeping_api.OPEN_TURNOVER_STATUSES:
        actions.append(("cancel", "Отменить", "btn-ghost"))
    return {
        "id": str(turnover.id),
        "room_number": turnover.room_number,
        "step_line": step_line(turnover, zone, now),
        "note": turnover.inspection_note,
        "actions": actions,
    }


def step_line(turnover: housekeeping_api.RoomTurnoverRead, zone: tzinfo, now: datetime) -> str:
    """«выезд 12:05 · Алия» — последний шаг пути, время по поясу отеля и кто."""
    label, moment, name = _last_step(turnover)
    line = f"{label} {clock(moment, zone, now)}"
    return f"{line} · {name}" if name else line


def _last_step(turnover: housekeeping_api.RoomTurnoverRead) -> tuple[str, datetime, str | None]:
    if turnover.ready_at is not None:
        return "готов", turnover.ready_at, turnover.accepted_by_display_name
    if turnover.cleaned_at is not None:
        return "убрано", turnover.cleaned_at, turnover.cleaned_by_display_name
    if turnover.cleaning_started_at is not None:
        return "уборка с", turnover.cleaning_started_at, turnover.cleaning_started_by_display_name
    if turnover.inspected_at is not None:
        return "проверено", turnover.inspected_at, turnover.inspected_by_display_name
    return "выезд", turnover.reported_at, turnover.reported_by_display_name


def clock(moment: datetime, zone: tzinfo, now: datetime) -> str:
    """«12:05» сегодня по отелю, иначе «24.10 12:05» — забытый путь виден сразу."""
    local = moment.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return f"{local:%H:%M}"
    return f"{local:%d.%m %H:%M}"


def _cleaned_line(cleaned: list[housekeeping_api.CleanedByStaff]) -> str:
    """«Сегодня убрано: 12 · Айгуль — 5, Дина — 4» (старшему и менеджеру)."""
    total = sum(row.count for row in cleaned)
    if not cleaned:
        return "Сегодня убрано: 0"
    people = ", ".join(f"{row.display_name} — {row.count}" for row in cleaned)
    return f"Сегодня убрано: {total} · {people}"

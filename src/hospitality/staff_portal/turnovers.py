"""Доска «Выезды и уборка» (spec 0046 §4–§6, issue #476): права, данные и отметка выезда.

Ресепшен отмечает выезд по номеру комнаты, хаускипинг ведёт номер до «готов к
заселению», старший хаускипинга принимает. Сам путь номера — модуль
`housekeeping` (ADR-021); здесь то, что знает только кабинет: кто какой шаг
вправе нажать (`rights_for` — одна функция на меню, кнопки и JSON-действия,
P-12), выселение гостя из кабинета вместе с отметкой выезда и вид доски.
Статус номера в OPERA кабинет не трогает (NG-2). Над группами — поздние выезды
сегодня (spec 0060 §2.3): гость ещё в номере, номер освободится позже обычного;
под ними — продления сегодня (spec 0062 §3, `turnover_extensions`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import DEFAULT_STAY_CHECKOUT_HOUR, TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience, StaffContext
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.platform.staff_team import staffed_service_keys
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import queue, turnover_deadline, turnover_extensions

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

LATE_CHECKOUT_HINT: Final = "Гость ещё в номере. Ресепшен отметит выезд, когда он сдаст ключ."

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
    # «Срочно к часу» (spec 0062 §2): ресепшен знает о раннем заезде, старшей
    # просьбу передают по телефону — те же, кто вправе «Отменить».
    can_set_deadline: bool


def rights_for(staff: StaffAudience) -> TurnoverRights:
    """Хаускипинг — шаги уборки; его старший — ещё приёмка, отмена и «срочно»;
    ресепшен — отметка выезда, отмена и «срочно»; менеджер — всё."""
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
        can_set_deadline=is_manager or reception or housekeeping_senior,
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
_GROUP_TITLES: Final = dict(_GROUPS)

# Группа «Срочно» (spec 0062 §2) — первой, выше «Ждут проверки».
URGENT_KEY: Final = "urgent"
URGENT_TITLE: Final = "Срочно"


def split_urgent(
    open_turnovers: list[housekeeping_api.RoomTurnoverRead],
) -> tuple[list[housekeeping_api.RoomTurnoverRead], list[housekeeping_api.RoomTurnoverRead]]:
    """Срочные (ранний срок сверху) и остальные: срочный путь стоит только в группе
    «Срочно», и плитка шага считает свою группу без него — плитка равна группе."""
    urgent = sorted(
        (turnover for turnover in open_turnovers if turnover.needed_by_at is not None),
        key=lambda turnover: (turnover.needed_by_at, turnover.reported_at),
    )
    rest = [turnover for turnover in open_turnovers if turnover.needed_by_at is None]
    return urgent, rest


def _step_counts(
    open_turnovers: list[housekeeping_api.RoomTurnoverRead],
) -> list[tuple[str, str, int]]:
    """Плитки над доской: «Срочно», затем шаги — `(ключ, название, сколько)`."""
    urgent, rest = split_urgent(open_turnovers)
    return [(URGENT_KEY, URGENT_TITLE, len(urgent))] + [
        (status.value, title, sum(1 for turnover in rest if turnover.status is status))
        for status, title in _GROUPS
    ]


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
    urgent, rest = split_urgent(open_turnovers)
    extensions = await turnover_extensions.housekeeping_extensions(open_turnovers, zone, now)
    extended = {stay.room_number: turnover_extensions.row_label(stay, zone) for stay in extensions}
    groups = [
        {
            "key": URGENT_KEY,
            "title": URGENT_TITLE,
            "rows": [
                _row(turnover, rights, zone, now, show_step=True, extended=extended)
                for turnover in urgent
            ],
        }
    ] + [
        {
            "key": status.value,
            "title": title,
            "rows": [
                _row(turnover, rights, zone, now, extended=extended)
                for turnover in rest
                if turnover.status is status
            ],
        }
        for status, title in _GROUPS
    ]
    ready_rows = [_row(turnover, rights, zone, now) for turnover in ready]
    # Плитки над доской (#639): «Срочно» и все шаги, и пустые — числом ноль,
    # «готовы» последней.
    steps = _step_counts(open_turnovers)
    steps.append((TurnoverStatus.READY.value, "Готовы сегодня", len(ready_rows)))
    cleaned = await housekeeping_api.count_cleaned_since(midnight) if rights.can_accept else []
    return {
        "tenant_slug": staff.tenant_slug,
        "rights": rights,
        "late_rows": await late_checkout_rows(config, zone, now),
        "late_hint": LATE_CHECKOUT_HINT,
        "housekeeping_ready": (
            await housekeeping_in_cabinet(staff) if with_form and rights.can_report else True
        ),
        "no_housekeeping_text": NO_HOUSEKEEPING_TEXT,
        "groups": [group for group in groups if group["rows"]],
        "extension_title": turnover_extensions.BLOCK_TITLE,
        "extension_rows": [turnover_extensions.block_line(stay, zone) for stay in extensions],
        "ready_rows": ready_rows,
        "steps": steps,
        "cleaned_line": _cleaned_line(cleaned) if rights.can_accept else None,
        "fragment_path": f"{board_path(staff.tenant_slug)}/fragment",
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/turnovers",
    }


async def board_counts(
    config: TenantConfig | None,
) -> tuple[list[tuple[str, str, int]], int]:
    """Числа доски для плитки главной (#606): непустые плитки `(ключ, название,
    сколько)` в порядке доски — «Срочно» и шаги — и «готовы сегодня» — теми же
    срезами и названиями, что у самой доски, чтобы плитка и доска не расходились (P-12)."""
    midnight = queue.hotel_midnight_utc(config)
    open_turnovers = await housekeeping_api.list_open_turnovers(limit=OPEN_LIMIT)
    ready = await housekeeping_api.list_turnovers_ready_since(midnight, limit=_READY_LIMIT)
    return [step for step in _step_counts(open_turnovers) if step[2]], len(ready)


async def late_checkout_rows(config: TenantConfig | None, zone: tzinfo, now: datetime) -> list[str]:
    """«704 · до 16:00» — поздние выезды сегодня (spec 0060 §2.3), по времени и номеру.

    Гость ещё в номере, а срок позже сегодняшнего расчётного часа и раньше
    следующей полуночи отеля (та же местная полночь, что `queue.hotel_midnight_utc`,
    от того же «сейчас»). Обычный выезд в расчётный час и завтрашний поздний —
    не здесь. Выезд оформлен — Stay закрыт, строка уходит сама.
    """
    checkout_hour = config.stay_checkout_hour if config is not None else DEFAULT_STAY_CHECKOUT_HOUR
    today = now.astimezone(zone).date()
    after = datetime.combine(today, time(checkout_hour), tzinfo=zone).astimezone(UTC)
    before = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    stays = await guests_api.list_stays_leaving_between(after, before)
    return [f"{stay.room_number} · до {stay.check_out_at.astimezone(zone):%H:%M}" for stay in stays]


def _row(
    turnover: housekeeping_api.RoomTurnoverRead,
    rights: TurnoverRights,
    zone: tzinfo,
    now: datetime,
    *,
    show_step: bool = False,
    extended: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Карточка номера; `show_step` — в группе «Срочно»: шаг точкой и подписью группы;
    `extended` — метки продления открытых путей по номеру комнаты (spec 0062 §3)."""
    status = turnover.status
    is_open = status in housekeeping_api.OPEN_TURNOVER_STATUSES
    needed_by = turnover.needed_by_at if is_open else None
    deadline: list[tuple[str, str, str]] = []
    if rights.can_set_deadline and is_open:
        if needed_by is None:
            deadline.append(("deadline", "Срочно к…", "btn-outline"))
        else:
            deadline += [
                ("deadline", "Изменить время", "btn-outline"),
                ("clear-deadline", "Снять срочность", "btn-quiet"),
            ]
    return {
        "id": str(turnover.id),
        "room_number": turnover.room_number,
        "step_line": step_line(turnover, zone, now),
        "step": (status.value, _GROUP_TITLES[status]) if show_step else None,
        "deadline": (
            turnover_deadline.board_label(needed_by, zone, now) if needed_by is not None else None
        ),
        "deadline_value": (
            turnover_deadline.clock_value(needed_by, zone)
            if needed_by is not None
            else turnover_deadline.default_clock(zone, now)
        ),
        "extended": (extended or {}).get(turnover.room_number) if is_open else None,
        "note": turnover.inspection_note,
        "actions": step_actions(status, rights, deadline),
    }


def step_actions(
    status: TurnoverStatus,
    rights: TurnoverRights,
    deadline: list[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Кнопки шага пути по статусу и правам: `(действие, надпись, класс кнопки)`.

    Одна таблица на доску выездов и панель номера доски «Номера» (spec 0061 §4):
    надписи и права двух страниц не расходятся (P-12). `deadline` — кнопки
    срочности карточки доски выездов (spec 0062), встают перед «Отменить»; они
    зависят от срока самого пути, поэтому панели «Номера», где кнопки общие
    на статус, их нет.
    """
    actions: list[tuple[str, str, str]] = []
    if rights.can_work and status is TurnoverStatus.REPORTED:
        actions += [("inspect", "Проверено", "btn-primary"), ("remark", "Замечание", "btn-outline")]
    if rights.can_work and status is TurnoverStatus.INSPECTED:
        actions.append(("start-cleaning", "Убираю", "btn-primary"))
    if rights.can_work and status is TurnoverStatus.CLEANING:
        actions.append(("finish-cleaning", "Убрано", "btn-primary"))
    if rights.can_accept and status is TurnoverStatus.CLEANED:
        actions.append(("accept", "Принять", "btn-primary"))
    if deadline:
        actions += deadline
    if rights.can_cancel and status in housekeeping_api.OPEN_TURNOVER_STATUSES:
        actions.append(("cancel", "Отменить", "btn-quiet"))
    return actions


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

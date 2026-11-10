"""Доска «Номера» (spec 0061, issues #666, #674): все номера отеля по этажам на живых данных.

Сменяет демо «Номера» (#603, #661). Список номеров — `TenantConfig.hotel_rooms`
(§2), этаж — `housekeeping_api.floor_of_room`, ряды этажа — стороны коридора
из того же списка, без подписей (#674, #905); занятость — активные проживания
(кабинет или Opera), работа — пути после выезда (spec 0046). Своих таблиц и
запросов у доски нет. Клетка показывает работу, а не статус номера в OPERA
(§6, NG-2). Статус клетки — чистая функция `build_room_board` (§3), страница
и фрагмент поллинга — `build_room_board_context`. Кнопки панели номера —
`turnovers.step_actions` и действия `turnover_api`: новых маршрутов, прав и
кодов ошибок нет (§4).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.hotel_rooms import HotelRoom
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.staff_portal import opera, queue, turnovers

StatusKey = Literal[
    "occupied",
    "due_out",
    "reported",
    "inspected",
    "cleaning",
    "cleaned",
    "ready",
    "vacant",
    "unknown",
]
TurnoverStatus = housekeeping_api.TurnoverStatus

# Ряды вне этажей (§3): номер без этажа («Люкс») и номер, которого нет в списке.
NO_FLOOR_KEY: Final = "none"
UNLISTED_KEY: Final = "unlisted"
# «Готовы сегодня» — с запасом на все номера крупного отеля, а не 200 доски выездов:
# номер, выпавший за потолок, стал бы «свободен» вместо «готов».
_READY_LIMIT: Final = turnovers.OPEN_LIMIT

UNLISTED_HINT: Final = (
    "Номер не совпал со списком отеля — проверьте, как его ввели при заселении или выезде."
)
EMPTY_LIST_TEXT: Final = (
    "Список номеров отеля ещё не загружен — на доске только номера с гостями и уборкой."
)
MANUAL_SOURCE_TEXT: Final = (
    "Свободные номера доска покажет после подключения Opera. Пока она знает только "
    "гостей, заселённых в кабинете, — остальные номера серые."
)
_GREY: Final = " — свободные номера пока серые."
UNKNOWN_LINE: Final = "В кабинете гостя нет — проверьте в Opera"


@dataclass(frozen=True)
class Status:
    key: StatusKey
    short: str  # подпись в клетке
    label: str  # подпись в панели номера
    counter: str  # счётчик над доской
    icon: str  # значок — имя макроса `_icons.html`


STATUSES: Final[tuple[Status, ...]] = (
    Status("occupied", "занят", "Занят", "Заняты", "bed"),
    Status("due_out", "выезд сегодня", "Выезд сегодня", "Выезд сегодня", "logout"),
    Status("reported", "ждёт проверки", "Выехал — ждёт проверки", "Ждут проверки", "search"),
    Status("inspected", "проверен", "Проверен — ждёт уборки", "Проверены", "check"),
    Status("cleaning", "уборка", "Уборка", "Убираются", "broom"),
    Status("cleaned", "ждёт приёмки", "Убран — ждёт приёмки", "Ждут приёмки", "clock"),
    Status("ready", "готов", "Готов к заселению", "Готовы", "key"),
    Status("vacant", "свободен", "Свободен", "Свободны", "door"),
    Status("unknown", "нет данных", "Нет данных о госте", "Нет данных", "eye-off"),
)
_BY_TURNOVER: Final[dict[TurnoverStatus, StatusKey]] = {
    TurnoverStatus.REPORTED: "reported",
    TurnoverStatus.INSPECTED: "inspected",
    TurnoverStatus.CLEANING: "cleaning",
    TurnoverStatus.CLEANED: "cleaned",
    TurnoverStatus.READY: "ready",
}
# «Отметить выезд» — у номера без открытого пути (§4): кто-то живёт или мог жить.
_REPORTABLE: Final[frozenset[StatusKey]] = frozenset({"occupied", "due_out", "vacant", "unknown"})


@dataclass(frozen=True)
class Cell:
    number: str
    status: StatusKey
    line: str  # строка под статусом в панели
    note: str = ""
    guests: int = 0  # 0 — в номере никто не живёт
    room_type: str = ""
    turnover_id: str = ""  # открытый путь — его шаги в панели


@dataclass(frozen=True)
class Row:
    key: str  # этаж числом, NO_FLOOR_KEY или UNLISTED_KEY
    title: str  # «14» у этажа, иначе название ряда
    place: str  # строка панели номера: «14 этаж»
    # Стороны коридора — ряд клеток на сторону, как на румплане (#674); подписи
    # стороны доска не пишет — слово основателя 10.11 (#905).
    sides: tuple[tuple[Cell, ...], ...]

    @property
    def is_floor(self) -> bool:
        return self.key not in (NO_FLOOR_KEY, UNLISTED_KEY)

    @property
    def cells(self) -> tuple[Cell, ...]:
        return tuple(cell for side in self.sides for cell in side)


@dataclass(frozen=True)
class RoomBoard:
    rows: tuple[Row, ...]
    counts: dict[StatusKey, int]
    total: int


def board_enabled() -> bool:
    """`ENABLE_ROOM_BOARD` и включённая доска выездов (§7): без неё кнопкам панели
    некуда вести. Одна проверка на меню, страницу и фрагмент."""
    return get_settings().enable_room_board and turnovers.board_enabled()


def board_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/rooms"


def knows_vacancy(config: TenantConfig | None, status: guests_api.PmsStatus | None) -> bool:
    """«Свободен» вправе показать только отель с Opera и свежим применённым снимком
    (§3 п. 4): заезд из Opera становится проживанием, лишь когда сверка снимок
    применила. Устаревший последний снимок тоже не годится — применённый тогда
    не новее его (находка ревью PR #671, П2-1)."""
    if config is None or config.pms_source != "opera_report" or status is None:
        return False
    return status.last_applied_at is not None and not status.applied_stale and not status.stale


def source_text(
    config: TenantConfig | None, status: guests_api.PmsStatus | None, now: datetime
) -> str | None:
    """Полоса над доской по источнику проживаний (§5), одна; None — номера не серые."""
    if knows_vacancy(config, status):
        return None
    if config is None or config.pms_source != "opera_report" or status is None:
        return MANUAL_SOURCE_TEXT
    zone = config.tzinfo
    applied = status.last_applied_at
    if applied is None:
        # Снимки приходят, а ни один не применён — не «не приходили» (П2-2 ревью PR #671).
        if status.taken_at is None:
            return "Данные из Opera ещё не приходили" + _GREY
        return "Данные из Opera приходят, но кабинет их пока не обработал" + _GREY
    since = min(applied, status.taken_at) if status.taken_at is not None else applied
    return f"Данные из Opera не обновлялись с {turnovers.clock(since, zone, now)}" + _GREY


def _notices(
    config: TenantConfig | None,
    status: guests_api.PmsStatus | None,
    now: datetime,
    *,
    has_rooms: bool,
) -> list[dict[str, str]]:
    """Полосы над доской: источник (у Opera с отказом — предупреждением) и пустой список."""
    notices = []
    text = source_text(config, status, now)
    if text is not None:
        opera_down = config is not None and config.pms_source == "opera_report"
        notices.append({"text": text, "tone": "warn" if opera_down else "info"})
    if not has_rooms:
        notices.append({"text": EMPTY_LIST_TEXT, "tone": "info"})
    return notices


def _key(number: str) -> str:
    """Совпадение номеров — строка в строку после обрезки крайних пробелов (§3)."""
    return number.strip()


def _natural(number: str) -> tuple[int, str]:
    """704 раньше 1204: номера вне списка — по длине, затем по тексту."""
    return len(number), number


def _stay_line(stay: guests_api.StayRead, local_now: datetime) -> tuple[StatusKey, str]:
    """«выезд сегодня» или «занят» и строка панели — по дате выезда в поясе отеля."""
    departure = stay.check_out_at.astimezone(local_now.tzinfo)
    if departure.date() > local_now.date():
        return "occupied", f"Гость до {departure:%d.%m}"
    if departure.date() == local_now.date():
        return "due_out", f"Выезд сегодня в {departure:%H:%M}"
    return "due_out", f"Выезд был {departure:%d.%m} в {departure:%H:%M}"


def _sides(cells: Sequence[tuple[str, Cell]]) -> tuple[tuple[Cell, ...], ...]:
    """Клетки ряда по сторонам коридора: стороны — в порядке первого номера списка,
    внутри стороны — порядок списка. Номера без стороны — свой ряд."""
    by_side: dict[str, list[Cell]] = {}
    for side, cell in cells:
        by_side.setdefault(side, []).append(cell)
    return tuple(tuple(group) for group in by_side.values())


def _cell(
    number: str,
    room_type: str,
    stay: guests_api.StayRead | None,
    open_turnover: housekeeping_api.RoomTurnoverRead | None,
    ready: housekeeping_api.RoomTurnoverRead | None,
    *,
    vacancy: bool,
    local_now: datetime,
) -> Cell:
    """Статус клетки — первое подходящее правило §3."""
    zone = local_now.tzinfo or UTC
    # 1. Открытый путь — кроме оставшегося от прошлого проживания (заселение позже выезда).
    if open_turnover is not None and not (
        stay is not None and stay.check_in_at > open_turnover.reported_at
    ):
        return Cell(
            number,
            _BY_TURNOVER[open_turnover.status],
            turnovers.step_line(open_turnover, zone, local_now),
            note=open_turnover.inspection_note or "",
            room_type=room_type,
            turnover_id=str(open_turnover.id),
        )
    # 2. Активное проживание.
    if stay is not None:
        status, line = _stay_line(stay, local_now)
        return Cell(number, status, line, guests=stay.guests_count, room_type=room_type)
    # 3. Принят сегодня — «готов» только при известной занятости (#688): гостя,
    # заселённого мимо кабинета, без Opera не видно. Иначе «нет данных» со следом работы.
    if ready is not None:
        line = turnovers.step_line(ready, zone, local_now)
        return Cell(number, "ready" if vacancy else "unknown", line, room_type=room_type)
    # 4. Свободен — только при свежем применённом снимке Opera, иначе «нет данных».
    if vacancy:
        return Cell(number, "vacant", "Гостя нет", room_type=room_type)
    return Cell(number, "unknown", UNKNOWN_LINE, room_type=room_type)


def build_room_board(
    rooms: Sequence[HotelRoom],
    stays: Iterable[guests_api.StayRead],
    open_turnovers: Iterable[housekeeping_api.RoomTurnoverRead],
    ready_today: Iterable[housekeeping_api.RoomTurnoverRead],
    *,
    vacancy_known: bool,
    local_now: datetime,
) -> RoomBoard:
    """Доска целиком: этажи по возрастанию, внутри — стороны коридора и порядок
    списка; затем «Без этажа» и «Нет в списке номеров». Список пуст — номера с
    гостями и уборкой по этажам. `local_now` — сейчас в поясе отеля; `ready_today`
    — свежие сверху."""
    stay_by_room: dict[str, guests_api.StayRead] = {}
    for stay in stays:
        stay_by_room.setdefault(_key(stay.room_number), stay)
    open_by_room: dict[str, housekeeping_api.RoomTurnoverRead] = {}
    for turnover in open_turnovers:
        open_by_room.setdefault(_key(turnover.room_number), turnover)
    ready_by_room: dict[str, housekeeping_api.RoomTurnoverRead] = {}
    for turnover in ready_today:
        ready_by_room.setdefault(_key(turnover.room_number), turnover)

    known = set(stay_by_room) | set(open_by_room) | set(ready_by_room)
    placed = [(room.number, room.room_type or "", room.side or "") for room in rooms] or [
        (number, "", "") for number in sorted(known, key=_natural)
    ]
    listed = {room.number for room in rooms}
    unlisted = sorted(known - listed, key=_natural) if rooms else []

    def cell(number: str, room_type: str = "") -> Cell:
        return _cell(
            number,
            room_type,
            stay_by_room.get(number),
            open_by_room.get(number),
            ready_by_room.get(number),
            vacancy=vacancy_known,
            local_now=local_now,
        )

    by_floor: dict[int | None, list[tuple[str, Cell]]] = {}
    for number, room_type, side in placed:
        floor = housekeeping_api.floor_of_room(number)
        by_floor.setdefault(floor, []).append((side, cell(number, room_type)))
    rows = [
        Row(str(floor), str(floor), f"{floor} этаж", _sides(by_floor[floor]))
        for floor in sorted(floor for floor in by_floor if floor is not None)
    ]
    if None in by_floor:
        rows.append(Row(NO_FLOOR_KEY, "Без этажа", "Без этажа", _sides(by_floor[None])))
    if unlisted:
        title = "Нет в списке номеров"
        rows.append(Row(UNLISTED_KEY, title, title, (tuple(cell(number) for number in unlisted),)))
    counts: Counter[StatusKey] = Counter(cell.status for row in rows for cell in row.cells)
    return RoomBoard(tuple(rows), dict(counts), sum(counts.values()))


def _steps(rights: turnovers.TurnoverRights, *, can_report: bool) -> dict[StatusKey, list[Any]]:
    """Кнопки панели по статусу клетки: «Отметить выезд» или шаги пути (§4)."""
    report = [("report", "Отметить выезд", "btn-primary")] if can_report else []
    steps: dict[StatusKey, list[Any]] = {key: list(report) for key in _REPORTABLE}
    for turnover_status, key in _BY_TURNOVER.items():
        steps[key] = turnovers.step_actions(turnover_status, rights)
    return steps


async def build_room_board_context(staff: StaffContext, *, with_steps: bool) -> dict[str, Any]:
    """Контекст доски. `with_steps` — страница: кнопки панели и есть ли хаускипинг
    в кабинете (без него кнопки «Отметить выезд» нет, ERR-HOUSEKEEPING-005);
    фрагмент поллинга (каждые 15 с) этих запросов не делает."""
    config = await queue.tenant_config(staff)
    now = utc_now()
    local_now = now.astimezone(config.tzinfo if config is not None else UTC)
    interval = config.pms_snapshot_interval_minutes if config is not None else None
    pms = await opera.read_status(
        interval if config is not None and config.pms_source == "opera_report" else None
    )
    vacancy = knows_vacancy(config, pms)
    rooms = config.hotel_rooms if config is not None else ()
    board = build_room_board(
        rooms,
        await guests_api.list_active_stays(),
        await housekeeping_api.list_open_turnovers(limit=turnovers.OPEN_LIMIT),
        await housekeeping_api.list_turnovers_ready_since(
            queue.hotel_midnight_utc(config), limit=_READY_LIMIT
        ),
        vacancy_known=vacancy,
        local_now=local_now,
    )
    # Свободных и готовых не бывает без Opera, «нет данных» — при свежей Opera:
    # счётчик, который всегда был бы нулём, не показывается.
    hidden: frozenset[StatusKey] = (
        frozenset({"unknown"}) if vacancy else frozenset({"vacant", "ready"})
    )
    statuses = [status for status in STATUSES if status.key not in hidden]
    context: dict[str, Any] = {
        "tenant_name": staff.tenant_name,
        "board": board,
        "statuses": [
            {"status": status, "count": board.counts.get(status.key, 0)} for status in statuses
        ],
        "status_by_key": {status.key: status for status in STATUSES},
        "notices": _notices(config, pms, now, has_rooms=bool(rooms)),
        "unlisted_key": UNLISTED_KEY,
        "unlisted_hint": UNLISTED_HINT,
        "fragment_path": f"{board_path(staff.tenant_slug)}/fragment",
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/turnovers",
    }
    if with_steps:
        rights = turnovers.rights_for(staff)
        can_report = rights.can_report and await turnovers.housekeeping_in_cabinet(staff)
        context["steps"] = _steps(rights, can_report=can_report)
    return context

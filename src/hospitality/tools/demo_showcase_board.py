"""Картина смены демо-отеля (#605): проживания, выезды на всех шагах доски, заявки.

Всё — сервисными функциями модулей (R-5), как кнопки кабинета: с событиями и
журналом изменений. Своё команда узнаёт без отдельной таблицы:
- проживания и выезды — по своим номерам (`DEMO_STAYS`, `DEMO_TURNOVERS`, этажи
  13–25 — демо-диапазон #603); номера вне списков она не читает и не трогает;
- заявки — по паре «номер + текст» из `DEMO_REQUESTS`.

Повторный запуск в тот же день отеля приводит к той же картине: что на месте —
остаётся, чего нет — создаётся. Остатки прошлых дней и, с `--reset`, вся прошлая
картина закрываются: проживание — выездом, путь номера — отменой, заявка — отменой
с причиной «дубль» (гостю такая отмена текста не шлёт). Стереть закрытое публичный
API не даёт, поэтому принятый сегодня номер второй раз не создаётся.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.tools.demo_showcase_staff import (
    RECEPTION_LOGIN,
    SENIOR_LOGIN,
    DemoActors,
    DemoShowcaseError,
)

TurnoverStatus = housekeeping_api.TurnoverStatus
RequestStatus = requests_api.RequestStatus

_CHECKOUT_HOUR: Final = 12
# Потолок скана своих строк: у демо-отеля их десятки, запас — на ручные пробы.
_SCAN_LIMIT: Final = 500


@dataclass(frozen=True)
class DemoStay:
    room: str
    guests: int
    nights: int  # 0 — выезд сегодня


@dataclass(frozen=True)
class DemoTurnover:
    room: str
    status: TurnoverStatus
    note: str | None = None


@dataclass(frozen=True)
class DemoRequest:
    service: str
    room: str
    summary: str
    status: RequestStatus
    by: str | None = None  # логин демо-сотрудника, нажавшего шаг; None — менеджер
    reason: requests_api.CancelReason | None = None


DEMO_STAYS: Final = (
    DemoStay("1301", 2, 2), DemoStay("1308", 1, 0), DemoStay("1405", 2, 3),
    DemoStay("1411", 2, 0), DemoStay("1512", 1, 1), DemoStay("1609", 3, 0),
    DemoStay("1702", 1, 2), DemoStay("1808", 2, 4), DemoStay("1915", 3, 2),
    DemoStay("2003", 1, 1), DemoStay("2207", 2, 3), DemoStay("2310", 1, 0),
    DemoStay("2501", 2, 5),
)  # fmt: skip

DEMO_TURNOVERS: Final = (
    DemoTurnover("1501", TurnoverStatus.REPORTED),
    DemoTurnover("1601", TurnoverStatus.REPORTED),
    DemoTurnover("1502", TurnoverStatus.INSPECTED, note="Нет полотенца"),
    DemoTurnover("1602", TurnoverStatus.INSPECTED),
    DemoTurnover("1503", TurnoverStatus.CLEANING),
    DemoTurnover("1603", TurnoverStatus.CLEANING),
    DemoTurnover("1504", TurnoverStatus.CLEANED),
    DemoTurnover("1505", TurnoverStatus.READY),
    DemoTurnover("1604", TurnoverStatus.READY),
)

OVERDUE_SUMMARY: Final = "Не работает кондиционер, в номере жарко"

DEMO_REQUESTS: Final = (
    DemoRequest("housekeeping", "1405", "Принести два полотенца и тапочки", RequestStatus.NEW),
    DemoRequest("maintenance", "1808", OVERDUE_SUMMARY, RequestStatus.NEW),
    DemoRequest("housekeeping", "1512", "Заменить постельное бельё", RequestStatus.IN_PROGRESS,
                by="DEMO15"),
    DemoRequest("it-support", "1702", "Не подключается Wi-Fi на ноутбуке",
                RequestStatus.IN_PROGRESS, by=RECEPTION_LOGIN),
    DemoRequest("fnb", "1301", "Завтрак в номер к 8:00: омлет и кофе", RequestStatus.DONE,
                by=RECEPTION_LOGIN),
    DemoRequest("maintenance", "2207", "Капает кран в ванной", RequestStatus.DONE),
    DemoRequest("supply", "1915", "Детская кроватка в номер", RequestStatus.CANCELLED,
                by=RECEPTION_LOGIN, reason=requests_api.CancelReason.UNAVAILABLE),
)  # fmt: skip

# Шаги пути номера после отметки выезда и путь заявки к целевому статусу.
_TURNOVER_STEPS: Final = (
    TurnoverStatus.INSPECTED,
    TurnoverStatus.CLEANING,
    TurnoverStatus.CLEANED,
    TurnoverStatus.READY,
)
_REQUEST_PATHS: Final[dict[RequestStatus, tuple[RequestStatus, ...]]] = {
    RequestStatus.NEW: (),
    RequestStatus.IN_PROGRESS: (RequestStatus.IN_PROGRESS,),
    RequestStatus.DONE: (RequestStatus.IN_PROGRESS, RequestStatus.DONE),
    RequestStatus.CANCELLED: (RequestStatus.CANCELLED,),
}


def _steps_to(status: TurnoverStatus) -> tuple[TurnoverStatus, ...]:
    """Шаги после отметки выезда до `status` включительно."""
    if status is TurnoverStatus.REPORTED:
        return ()
    return _TURNOVER_STEPS[: _TURNOVER_STEPS.index(status) + 1]


def check_out_moment(nights: int, zone: ZoneInfo, now: datetime) -> datetime:
    """Выезд в 12:00 отеля через `nights` ночей (UTC). «Сегодня», а полдень прошёл, —
    через час-два ровно в час, но не позже 23:59: заселение в прошлое ядро отвергнет,
    а «выезд сегодня» не должен уехать на завтра."""
    local_now = now.astimezone(zone)
    moment = datetime.combine(
        local_now.date() + timedelta(days=nights), time(_CHECKOUT_HOUR), tzinfo=zone
    )
    if moment <= now:
        later = (local_now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        end_of_day = datetime.combine(local_now.date(), time(23, 59), tzinfo=zone)
        moment = min(later, end_of_day) if end_of_day > now else later
    return moment.astimezone(UTC)


async def ensure_stays(actors: DemoActors, *, zone: ZoneInfo, now: datetime, reset: bool) -> str:
    """Проживания в своих номерах: живое остаётся, просроченное — выезд и новое заселение."""
    reception = actors.of(RECEPTION_LOGIN)
    created = 0
    for spec in DEMO_STAYS:
        stay = await guests_api.find_active_stay(spec.room)
        if stay is not None and (reset or stay.check_out_at <= now):
            await guests_api.check_out(stay.id, acting_user=reception)
            stay = None
        if stay is None:
            await guests_api.check_in(
                guests_api.StayCheckIn(
                    room_number=spec.room,
                    check_out_at=check_out_moment(spec.nights, zone, now),
                    guests_count=spec.guests,
                ),
                acting_user=reception,
            )
            created += 1
    today = sum(1 for spec in DEMO_STAYS if spec.nights == 0)
    return f"Проживания: {len(DEMO_STAYS)} (выезд сегодня — {today}), заселено сейчас — {created}."


async def ensure_turnovers(actors: DemoActors, *, midnight: datetime, reset: bool) -> str:
    """Пути номеров на всех шагах доски. Свой путь не того шага или вчерашний — отмена и новый."""
    open_paths = {
        path.room_number: path
        for path in await housekeeping_api.list_open_turnovers(limit=_SCAN_LIMIT)
    }
    ready_today = {
        path.room_number
        for path in await housekeeping_api.list_turnovers_ready_since(midnight, limit=_SCAN_LIMIT)
    }
    created = 0
    for spec in DEMO_TURNOVERS:
        current = open_paths.get(spec.room)
        if current is not None:
            if not reset and current.status is spec.status and current.reported_at >= midnight:
                continue
            await housekeeping_api.change_turnover_status(current.id, TurnoverStatus.CANCELLED)
        elif spec.status is TurnoverStatus.READY and spec.room in ready_today:
            continue
        report = await housekeeping_api.report_checkout(
            spec.room, acting_user=actors.of(RECEPTION_LOGIN)
        )
        for step in _steps_to(spec.status):
            await housekeeping_api.change_turnover_status(
                report.turnover.id,
                step,
                acting_user=(
                    actors.of(SENIOR_LOGIN)
                    if step is TurnoverStatus.READY
                    else actors.maid_for(spec.room)
                ),
                inspection_note=spec.note if step is TurnoverStatus.INSPECTED else None,
            )
        created += 1
    return (
        f"Выезды и уборка: {len(DEMO_TURNOVERS)} номеров на всех шагах, создано сейчас — {created}."
    )


async def ensure_requests(actors: DemoActors, *, midnight: datetime, reset: bool) -> str:
    """Заявки разных служб и статусов, как форма «Новая заявка» кабинета."""
    categories = {category.key: category.id for category in await requests_api.list_categories()}
    missing = sorted({spec.service for spec in DEMO_REQUESTS} - categories.keys())
    if missing:
        raise DemoShowcaseError(
            f"У демо-отеля нет служб {', '.join(missing)} — "
            "сначала python -m hospitality.tools.seed."
        )
    ours = {(spec.room, spec.summary) for spec in DEMO_REQUESTS}
    for room in sorted({spec.room for spec in DEMO_REQUESTS}):
        for request in await requests_api.list_open_requests(limit=_SCAN_LIMIT, room_number=room):
            if (room, request.summary) in ours and (reset or request.created_at < midnight):
                await requests_api.change_request_status(
                    request.id,
                    RequestStatus.CANCELLED,
                    cancel_reason=requests_api.CancelReason.DUPLICATE,
                    acting_user=actors.manager,
                )
    # Сегодняшняя картина, кроме погашенного «дублем» выше; список свежие сверху —
    # reversed, чтобы при нескольких одинаковых брать самую свежую.
    created_today = await requests_api.list_requests_created_since(
        created_after=midnight, limit=_SCAN_LIMIT
    )
    today = {
        (request.room_number, request.summary): request
        for request in reversed(created_today)
        if (request.room_number, request.summary) in ours
        and request.cancel_reason is not requests_api.CancelReason.DUPLICATE
    }
    created = 0
    for spec in DEMO_REQUESTS:
        existing = today.get((spec.room, spec.summary))
        if existing is None:
            existing = await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=categories[spec.service],
                    origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
                    summary=spec.summary,
                    room_number=spec.room,
                    guest_language="ru",
                ),
                acting_user=actors.of(RECEPTION_LOGIN),
            )
            created += 1
        await _advance_request(existing, spec, actors)
    return f"Заявки: {len(DEMO_REQUESTS)} в разных службах и статусах, создано сейчас — {created}."


async def _advance_request(
    request: requests_api.ServiceRequestRead, spec: DemoRequest, actors: DemoActors
) -> None:
    """Довести заявку до статуса картины, если путь ещё впереди (оборванный запуск)."""
    path = _REQUEST_PATHS[spec.status]
    reachable = (RequestStatus.NEW, *path)
    if request.status not in reachable:
        return  # заявку двинули руками дальше картины — пусть остаётся как есть
    actor = actors.of(spec.by) if spec.by is not None else actors.manager
    for step in path[reachable.index(request.status) :]:
        await requests_api.change_request_status(
            request.id,
            step,
            cancel_reason=spec.reason if step is RequestStatus.CANCELLED else None,
            acting_user=actor,
        )

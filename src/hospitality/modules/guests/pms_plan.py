"""План сверки снимка PMS с проживаниями тенанта (spec 0058 §2.2–§2.3, ADR-004 п. 1–5).

Чистая функция без БД: на входе строки снимка, активные Stay и брони, чей Stay
уже закрыт; на выходе — что сделать и что показать ресепшену. Исполняет план
`pms_sync.py`, по одному Stay в транзакции, в порядке полей `SyncPlan`: выезды,
переселения, сроки, заезды — освободившаяся комната успевает освободиться до
заезда в неё. Занятость комнат план ведёт сам, по тому же порядку, поэтому
исполнение не упирается в комнату, которую план считал свободной.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Final

from hospitality.modules.guests.models import StaySource
from hospitality.modules.guests.schemas import (
    MAX_GUESTS_PER_STAY,
    PmsDiscrepancy,
    PmsDiscrepancyKind,
)
from hospitality.shared.ports.pms import PmsStayRow

# Защита от массового закрытия (§2.3): выездов в снимке больше этой доли
# активных PMS-Stay И больше этого числа — выезды снимка придерживаются.
MASS_CLOSURE_MIN: Final = 10
MASS_CLOSURE_SHARE: Final = 0.2


@dataclass(frozen=True)
class HouseStay:
    """Активный Stay тенанта — то, что о нём нужно плану."""

    id: uuid.UUID
    room_number: str
    source: StaySource
    booking_ref: str | None
    check_in_at: datetime
    check_out_at: datetime
    missed_snapshots: int


@dataclass(frozen=True)
class CheckIn:
    row: PmsStayRow
    check_out_at: datetime


@dataclass(frozen=True)
class Adopt:
    """Ручной Stay этой брони становится PMS-Stay; `check_out_at` None — срок прежний."""

    stay_id: uuid.UUID
    room_number: str
    row: PmsStayRow
    check_out_at: datetime | None


@dataclass(frozen=True)
class Move:
    stay_id: uuid.UUID
    booking_ref: str
    from_room: str
    to_room: str


@dataclass(frozen=True)
class Extend:
    stay_id: uuid.UUID
    booking_ref: str
    check_out_at: datetime


@dataclass(frozen=True)
class CheckOut:
    stay_id: uuid.UUID
    booking_ref: str


@dataclass
class SyncPlan:
    check_outs: list[CheckOut] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    extends: list[Extend] = field(default_factory=list)
    check_ins: list[CheckIn] = field(default_factory=list)
    adoptions: list[Adopt] = field(default_factory=list)
    discrepancies: list[PmsDiscrepancy] = field(default_factory=list)
    # Счётчики пропусков — одной транзакцией в конце прогона (§2.2).
    reset_missed: list[uuid.UUID] = field(default_factory=list)
    add_missed: list[uuid.UUID] = field(default_factory=list)
    # Сколько выездов придержано защитой (§2.3); 0 — защита не сработала.
    held_closures: int = 0


def checkout_moment(departure: date, *, zone: tzinfo, checkout_hour: int) -> datetime:
    """Срок Stay брони: расчётный час дня выезда по поясу отеля, в UTC (§9)."""
    return datetime.combine(departure, time(checkout_hour), tzinfo=zone).astimezone(UTC)


def plan_sync(
    rows: Iterable[PmsStayRow],
    stays: Iterable[HouseStay],
    closed_refs: frozenset[str],
    *,
    zone: tzinfo,
    checkout_hour: int,
    now: datetime,
) -> SyncPlan:
    """Разложить снимок в план по правилам ADR-004 п. 1–5 и spec 0058 §2.2–§2.3."""
    house = list(stays)
    pms_by_ref: dict[str, HouseStay] = {}
    for stay in house:
        if stay.source is StaySource.PMS and stay.booking_ref is not None:
            pms_by_ref[stay.booking_ref] = stay
    occupant: dict[str, HouseStay] = {s.room_number: s for s in house}
    by_ref = {row.booking_ref: row for row in _merge_sharers(rows, pms_by_ref)}
    plan = SyncPlan()

    def target(row: PmsStayRow) -> datetime:
        return checkout_moment(row.departure, zone=zone, checkout_hour=checkout_hour)

    def leaving(stay: HouseStay | None) -> bool:
        """PMS-Stay, которого в снимке нет: его комнату освободит выезд (п. 3) — ждём."""
        return stay is not None and stay.source is StaySource.PMS and stay.booking_ref not in by_ref

    # Выезды (п. 3) и защита от массового закрытия (п. 5).
    absent = {ref: s for ref, s in pms_by_ref.items() if ref not in by_ref}
    if len(absent) > MASS_CLOSURE_MIN and len(absent) > MASS_CLOSURE_SHARE * len(pms_by_ref):
        plan.held_closures = len(absent)
    else:
        for ref, stay in absent.items():
            if stay.missed_snapshots >= 1:
                plan.check_outs.append(CheckOut(stay.id, ref))
                del occupant[stay.room_number]
            else:
                plan.add_missed.append(stay.id)
    plan.reset_missed = [
        s.id for ref, s in pms_by_ref.items() if ref in by_ref and s.missed_snapshots
    ]

    # Переселения (п. 2): пока ход есть — цепочка «305 → 410, 410 → 520» проходит
    # в любом порядке строк; не прошедшие — расхождение или ожидание выезда.
    pending = [
        (moving, row)
        for ref, row in by_ref.items()
        if (moving := pms_by_ref.get(ref)) is not None and moving.room_number != row.room_number
    ]
    while pending:
        blocked = []
        for stay, row in pending:
            if occupant.get(row.room_number) is None:
                plan.moves.append(Move(stay.id, row.booking_ref, stay.room_number, row.room_number))
                del occupant[stay.room_number]
                occupant[row.room_number] = stay
            else:
                blocked.append((stay, row))
        if len(blocked) == len(pending):
            break
        pending = blocked
    for stay, row in pending:
        if not leaving(occupant.get(row.room_number)):
            plan.discrepancies.append(
                PmsDiscrepancy(
                    kind=PmsDiscrepancyKind.MOVE_BLOCKED,
                    room_number=stay.room_number,
                    booking_ref=row.booking_ref,
                    target_room=row.room_number,
                )
            )

    # Сроки (п. 2): та же дата выезда — час из кабинета не трогается; прошлое — нет.
    for ref, row in by_ref.items():
        matched = pms_by_ref.get(ref)
        if matched is None or matched.check_out_at.astimezone(zone).date() == row.departure:
            continue
        if (new_check_out := target(row)) > now:
            plan.extends.append(Extend(matched.id, ref, new_check_out))

    # Заезды и усыновление (п. 1).
    for ref, row in by_ref.items():
        if ref in pms_by_ref or ref in closed_refs:
            continue
        holder = occupant.get(row.room_number)
        if holder is None:
            plan.check_ins.append(CheckIn(row, target(row)))
            continue
        if holder.source is StaySource.MANUAL:
            _adopt_or_flag(plan, holder, row, zone=zone, new_check_out=target(row), now=now)
        elif not leaving(holder):
            plan.discrepancies.append(
                PmsDiscrepancy(
                    kind=PmsDiscrepancyKind.OCCUPIED_BY_PMS,
                    room_number=row.room_number,
                    booking_ref=ref,
                    other_booking_ref=holder.booking_ref,
                )
            )
    return plan


def _adopt_or_flag(
    plan: SyncPlan,
    holder: HouseStay,
    row: PmsStayRow,
    *,
    zone: tzinfo,
    new_check_out: datetime,
    now: datetime,
) -> None:
    """Ручной Stay в комнате брони: та же бронь по дате заезда — усыновить, иначе расхождение.

    Opera пишет `arrival` бизнес-датой, а ночной аудит идёт после полуночи:
    заехавший в 01:00 по Opera заехал вчера — поэтому годится и следующий день.
    """
    checked_in = holder.check_in_at.astimezone(zone).date()
    if checked_in not in (row.arrival, row.arrival + timedelta(days=1)):
        plan.discrepancies.append(
            PmsDiscrepancy(
                kind=PmsDiscrepancyKind.OCCUPIED_BY_MANUAL,
                room_number=row.room_number,
                booking_ref=row.booking_ref,
                arrival=row.arrival,
                manual_check_in=checked_in,
            )
        )
        return
    same_day = holder.check_out_at.astimezone(zone).date() == row.departure
    keep = same_day or new_check_out <= now
    plan.adoptions.append(
        Adopt(holder.id, holder.room_number, row, None if keep else new_check_out)
    )


def _merge_sharers(
    rows: Iterable[PmsStayRow], pms_by_ref: dict[str, HouseStay]
) -> list[PmsStayRow]:
    """Две брони в одной комнате (sharer в Opera) — одна строка (§2.2).

    Номер брони — того, кто уже ведёт PMS-Stay комнаты (иначе новый sharer с
    меньшим номером закрыл бы Stay и сессии живущего гостя), без него — меньший;
    выезд — поздний, гости — сумма (`None`, если у кого-то пусто).
    """
    holding = {stay.room_number: ref for ref, stay in pms_by_ref.items()}
    by_room: dict[str, list[PmsStayRow]] = defaultdict(list)
    for row in rows:
        by_room[row.room_number].append(row)
    merged = []
    for room, group in by_room.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        refs = {row.booking_ref for row in group}
        held_ref = holding.get(room)
        ref = (
            held_ref if held_ref is not None and held_ref in refs else min(refs, key=_booking_order)
        )
        counts = [row.guests_count for row in group]
        guests = None if None in counts else min(sum(c or 0 for c in counts), MAX_GUESTS_PER_STAY)
        merged.append(
            PmsStayRow(
                room_number=room,
                booking_ref=ref,
                arrival=min(row.arrival for row in group),
                departure=max(row.departure for row in group),
                guests_count=guests,
            )
        )
    return merged


def _booking_order(ref: str) -> tuple[int, int, str]:
    """«Меньший номер» брони: числовой — по числу, прочие — после, по строке."""
    return (0, int(ref), ref) if ref.isdigit() else (1, 0, ref)

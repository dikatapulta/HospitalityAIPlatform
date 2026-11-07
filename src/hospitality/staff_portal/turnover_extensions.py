"""Продление для хаускипинга на доске выездов (spec 0062 §3): правило и надписи.

Модуль `guests` отдаёт факты — Stay, продлённые с полуночи отеля, и срок до
продления (`extended_from`). Что из этого важно хаускипингу, решает кабинет —
здесь, одной функцией на доску, сигнал и пуш (P-12): новая дата выезда позже
сегодняшней и одно из двух — выезд до продления был сегодня или раньше (горничная
ждёт выезда), или у номера открыт путь на доске (уборку начали, а гость остаётся).
Поздний выезд в тот же день (spec 0060) продлением не считается: дата та же.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, tzinfo
from typing import TYPE_CHECKING

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api

if TYPE_CHECKING:  # turnovers и floors импортируют этот модуль — нужны только типы
    from hospitality.staff_portal.floors import FloorRouting
    from hospitality.staff_portal.turnovers import TurnoverRights

BLOCK_TITLE = "Продлили — выезда не будет"


async def housekeeping_extensions(
    open_turnovers: Iterable[housekeeping_api.RoomTurnoverRead], zone: tzinfo, now: datetime
) -> list[guests_api.StayRead]:
    """Продления сегодня, которые касаются хаускипинга, — по номеру комнаты."""
    today = now.astimezone(zone).date()
    midnight = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    open_rooms = {turnover.room_number for turnover in open_turnovers}
    stays = await guests_api.list_recent_extensions(midnight)
    return [stay for stay in stays if _concerns_housekeeping(stay, zone, today, open_rooms)]


async def extensions_calling(
    open_turnovers: Iterable[housekeeping_api.RoomTurnoverRead],
    rights: TurnoverRights,
    routing: FloorRouting,
    zone: tzinfo,
    now: datetime,
) -> list[guests_api.StayRead]:
    """Продления, которые зовут этого сотрудника (сигнал и пуш, вид `extended`):
    старших хаускипинга — все, горничную — номера её первой ступени (этаж, нет
    команды — вся служба), как срочный номер до «Убрано» (spec 0062 §2). Ресепшен
    продление не зовёт — и список продлений для него не читается.
    """
    if not (rights.can_work or rights.can_accept):
        return []
    return [
        stay
        for stay in await housekeeping_extensions(open_turnovers, zone, now)
        if rights.can_accept or routing.calls_me_first(stay.room_number)
    ]


def _concerns_housekeeping(
    stay: guests_api.StayRead, zone: tzinfo, today: date, open_rooms: set[str]
) -> bool:
    if stay.extended_from is None or stay.check_out_at.astimezone(zone).date() <= today:
        return False
    due_today = stay.extended_from.astimezone(zone).date() <= today
    return due_today or stay.room_number in open_rooms


def until(stay: guests_api.StayRead, zone: tzinfo) -> str:
    """«07.11» — новая дата выезда по отелю."""
    return f"{stay.check_out_at.astimezone(zone):%d.%m}"


def block_line(stay: guests_api.StayRead, zone: tzinfo) -> str:
    """Строка блока «Продлили — выезда не будет»: «1504 — продлено до 07.11»."""
    return f"{stay.room_number} — продлено до {until(stay, zone)}"


def row_label(stay: guests_api.StayRead, zone: tzinfo) -> str:
    """Метка открытого пути номера: «Продлено до 07.11 — гость остаётся»."""
    return f"Продлено до {until(stay, zone)} — гость остаётся"

"""Страница «Этажи» (spec 0053 §5, issue #478; доска — #660): контекст `floors.html`.

Права, раздача и адресация — `floors.py`; здесь то, что рисует страница: доска
старшей хаускипинга и менеджеру (список горничных и сетка этажей с карточками —
перетаскивание и «нажать, потом этаж» ведёт `static/floors.js`), «По этажам»
остальным, время раздачи и строка горничной. Сколько этажей у отеля, кабинет не
знает: доска рисует этажи подряд от нижнего до верхнего известного — этажи
раздачи и номеров с действующим проживанием (§5).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any, Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform import staff_team
from hospitality.platform.models import MembershipStatus, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import plural
from hospitality.staff_portal import floors, queue

NO_HOUSEKEEPING_TEXT: Final = (
    "В кабинете пока нет сотрудников хаускипинга. Менеджер отметит им службу на странице "
    "«Сотрудники»."
)


async def build_floors_context(staff: StaffContext, *, saved: bool) -> dict[str, Any]:
    """Доска раздачи старшему и менеджеру, «По этажам» остальным (§5).

    На доске и в списке — только активные сотрудники уборки: строки раздачи
    отключённого не действуют и при следующем сохранении уходят.
    """
    rights = floors.rights_for(staff)
    members = [
        member
        for member in await staff_team.list_tenant_members(staff.tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and HOUSEKEEPING_SERVICE_KEY in member.service_keys
    ]
    names = {member.user_id: member.display_name for member in members}
    assignments = [
        row for row in await housekeeping_api.list_floor_assignments() if row.user_id in names
    ]
    by_user: dict[uuid.UUID, set[int]] = {}
    by_floor: dict[int, list[str]] = {}
    for row in assignments:
        by_user.setdefault(row.user_id, set()).add(row.floor)
        by_floor.setdefault(row.floor, []).append(names[row.user_id])
    config = await queue.tenant_config(staff)
    zone: tzinfo = config.tzinfo if config is not None else UTC
    assigned_at = max((row.assigned_at for row in assignments), default=None)
    show_board = rights.can_edit and bool(members)
    return {
        "tenant_slug": staff.tenant_slug,
        "rights": rights,
        "show_board": show_board,
        "saved": saved and rights.can_edit,
        "has_housekeeping": bool(members),
        "no_housekeeping_text": NO_HOUSEKEEPING_TEXT,
        # Не розданы — строки нет: это говорит пустое состояние «По этажам» (#639).
        "status_line": (
            distributed_line(assigned_at, zone, utc_now()) if assigned_at is not None else None
        ),
        "my_line": (
            _my_line(frozenset(by_user.get(staff.user_id, ()))) if staff.user_id in names else None
        ),
        "members": [
            {
                "user_id": str(member.user_id),
                "name": member.display_name,
                "floors_line": floors_line(frozenset(by_user.get(member.user_id, ()))),
            }
            for member in members
        ],
        "board": await _board(by_user, names) if show_board else [],
        "by_floor": [
            {"floor": floor, "names": ", ".join(sorted(people, key=str.lower))}
            for floor, people in sorted(by_floor.items())
        ],
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/floors",
    }


def board_floors(known: Iterable[int]) -> list[int]:
    """Этажи доски — подряд от нижнего до верхнего известного: пустой этаж между
    занятыми тоже нужен, на него и перетаскивают. Ничего не известно — доска пуста,
    этаж добавляют руками (§5)."""
    floors_known = set(known)
    if not floors_known:
        return []
    return list(range(min(floors_known), max(floors_known) + 1))


async def _board(
    by_user: Mapping[uuid.UUID, set[int]], names: Mapping[uuid.UUID, str]
) -> list[dict[str, Any]]:
    """Сетка этажей с карточками горничных: этажи раздачи и номеров с действующим
    проживанием (`floor_of_room`, §3). Из проживания берётся только номер комнаты."""
    known = {floor for user_floors in by_user.values() for floor in user_floors}
    for stay in await guests_api.list_active_stays():
        floor = housekeeping_api.floor_of_room(stay.room_number)
        if floor is not None:
            known.add(floor)
    cards: dict[int, list[dict[str, str]]] = {}
    for user_id, user_floors in by_user.items():
        for floor in user_floors:
            cards.setdefault(floor, []).append({"user_id": str(user_id), "name": names[user_id]})
    return [
        {"floor": floor, "cards": sorted(cards.get(floor, []), key=lambda c: c["name"].lower())}
        for floor in board_floors(known)
    ]


def floors_line(user_floors: frozenset[int]) -> str:
    """Подпись горничной в списке доски — та же, что пересчитывает `floors.js`:
    «этаж 7», «этажи 7, 8, 12-14», «без этажа»."""
    if not user_floors:
        return "без этажа"
    if len(user_floors) == 1:
        return f"этаж {next(iter(user_floors))}"
    return f"этажи {floors.format_floors(user_floors)}"


def distributed_line(assigned_at: datetime, zone: tzinfo, now: datetime) -> str:
    """«Раздано сегодня в 08:15» / «Раздано 31.10 в 08:15 · 2 дня назад» по поясу отеля:
    смена раз в два дня не должна забываться (§5)."""
    local = assigned_at.astimezone(zone)
    days = (now.astimezone(zone).date() - local.date()).days
    if days == 0:
        return f"Раздано сегодня в {local:%H:%M}"
    stamp = f"Раздано {local:%d.%m} в {local:%H:%M}"
    if days < 0:  # часы сервера разошлись — дата без «назад»
        return stamp
    if days == 1:
        return f"{stamp} · вчера"
    return f"{stamp} · {days} {plural(days, 'день', 'дня', 'дней')} назад"


def _my_line(user_floors: frozenset[int]) -> str:
    if not user_floors:
        return "Сейчас у вас нет этажей — вас позовут, если заявку не взяли вовремя."
    return (
        f"Ваши этажи: {floors.format_floors(user_floors)} — "
        "заявки и выезды отсюда зовут сначала вас."
    )

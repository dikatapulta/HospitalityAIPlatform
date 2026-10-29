"""Раздача этажей хаускипинга (spec 0053 §3, §6, issue #478).

Этаж номера — правило нумерации, а не настройка: номер из 3–4 цифр без двух
последних. Раздача — какие этажи старшая дала каким горничным; модуль хранит её
и заменяет целиком. Кто из горничных сейчас активен и в службе уборки, модуль не
знает (членство — платформенная таблица): строки фильтрует кабинет
(`staff_portal/floors.py`). Каждая функция — одна транзакция в `tenant_context`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Final

from sqlalchemy import delete, insert, select, text

from hospitality.modules.housekeeping.models import FLOOR_MAX, FLOOR_MIN, FloorAssignment
from hospitality.modules.housekeeping.schemas import FloorAssignmentRead
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# 704 → 7, 1204 → 12, 2601 → 26: этаж и две цифры номера на этаже. Только ASCII-
# цифры: `str.isdigit` пропустил бы «²» и арабские цифры.
_ROOM_WITH_FLOOR: Final = re.compile(r"[0-9]{3,4}")


def floor_of_room(room_number: str | None) -> int | None:
    """Этаж номера комнаты; None — этажа нет («Люкс», «12», «050»): зовут всю службу."""
    if room_number is None:
        return None
    room = room_number.strip()
    if _ROOM_WITH_FLOOR.fullmatch(room) is None:
        return None
    floor = int(room) // 100
    return floor if FLOOR_MIN <= floor <= FLOOR_MAX else None


async def list_floor_assignments() -> list[FloorAssignmentRead]:
    """Вся раздача отеля, по этажу и сотруднику — и для страницы, и для сигнала."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(FloorAssignment).order_by(FloorAssignment.floor, FloorAssignment.user_id)
        )
        return [FloorAssignmentRead.model_validate(row) for row in rows]


async def replace_floor_assignments(floors_by_user: Mapping[uuid.UUID, frozenset[int]]) -> int:
    """«Сохранить раздачу»: прежняя раздача отеля удаляется, новая пишется целиком.

    Вернуть число строк. Пустой набор этажей у сотрудника — без этажа. Два
    одновременных сохранения не смешиваются: `LOCK TABLE … EXCLUSIVE` ставит
    второе в очередь за первым, и побеждает последнее целиком (spec 0053 §5);
    чтение сигналом этот замок не ждёт. Проверка, что сотрудники — горничные
    этого отеля и этажи в границах, — у вызывающего; CHECK в базе — страховка.
    """
    now = utc_now()
    rows = [
        {"user_id": user_id, "floor": floor, "assigned_at": now}
        for user_id, floors in floors_by_user.items()
        for floor in sorted(floors)
    ]
    async with session_scope() as session:
        await session.execute(text(f"LOCK TABLE {FloorAssignment.__tablename__} IN EXCLUSIVE MODE"))
        # Явный tenant_id: RLS и так режет чужие строки, но удаление «всех строк»
        # не должно зависеть только от политики.
        tenant_id = current_tenant_id()
        await session.execute(delete(FloorAssignment).where(FloorAssignment.tenant_id == tenant_id))
        if rows:
            await session.execute(
                insert(FloorAssignment), [{"tenant_id": tenant_id, **row} for row in rows]
            )
    logger.info("housekeeping.floors_replaced", members=len(floors_by_user), rows=len(rows))
    return len(rows)

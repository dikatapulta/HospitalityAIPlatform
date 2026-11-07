"""Лестница невзятой заявки (spec 0045 §4): кого зовёт сигнал кабинета.

| Ступень | Когда | Кого зовёт |
|---|---|---|
| 1. Служба | с создания | сотрудников и старших службы заявки |
| 2. Просрочена | через `d` | их же: «Заявка #12 ждёт уже 30 мин» |
| 3. Ресепшен и менеджер | через `2d` | ещё и ресепшен с менеджером: «Никто не взял заявку #12» |

Ступень добавляет адресатов, а не заменяет: служба слышит свою заявку и на
третьей ступени. Сразу на третью идёт срочная (spec 0034), эскалация
(spec 0039, 0040), заявка службы без единого активного сотрудника — звать
некого — и заявка, поднятая напоминаниями гостя (`guest_raised`, spec 0070 §5).
`d` — `TenantConfig.reminder_delay_for(категория)`, тот же срок, что у метки
«просрочена» (P-12); выключенный срок (`None`) снимает ступени по времени,
четыре «сразу» остаются. Возраст — от создания заявки (spec 0042 §2.1).

Чистые функции без БД: числа на входе, ступень на выходе.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from typing import Final

# Во сколько сроков `d` заявка поднимается к ресепшену и менеджеру.
RECEPTION_DELAY_FACTOR: Final = 2


class LadderStep(enum.IntEnum):
    """Ступень лестницы: чем выше, тем шире круг тех, кого зовёт заявка."""

    SERVICE = 1
    OVERDUE = 2
    RECEPTION = 3


def step_by_time(age: timedelta, delay: timedelta | None) -> LadderStep:
    """Ступень по одному возрасту: `d` — просрочена, `2d` — ресепшен и менеджер.

    Границы включительны — как у метки «просрочена» (`queue.is_overdue` зовёт
    эту же функцию). Срок выключен — ступеней по времени нет.
    """
    if delay is None:
        return LadderStep.SERVICE
    if age >= RECEPTION_DELAY_FACTOR * delay:
        return LadderStep.RECEPTION
    if age >= delay:
        return LadderStep.OVERDUE
    return LadderStep.SERVICE


def ladder_step(
    *,
    age: timedelta,
    delay: timedelta | None,
    is_urgent: bool,
    is_escalation: bool,
    service_staffed: bool,
    is_guest_raised: bool = False,
) -> LadderStep:
    """Ступень невзятой заявки с учётом четырёх «сразу на третью»."""
    if is_urgent or is_escalation or not service_staffed or is_guest_raised:
        return LadderStep.RECEPTION
    return step_by_time(age, delay)


def guest_raised(
    *,
    reminder_count: int,
    reminded_at: datetime | None,
    created_at: datetime,
    claimed_at: datetime | None,
    is_claimed: bool,
    raise_after: int | None,
    delay: timedelta | None,
    stalled_delay: timedelta | None,
) -> bool:
    """Подняли ли заявку напоминания гостя к ресепшену и менеджеру (spec 0070 §5).

    Порог отеля выключен (`raise_after is None`) или напоминаний не было — нет.
    Иначе — да, если напоминаний не меньше порога или последнее пришло уже
    просроченной: невзятой — через `d` от создания (граница `step_by_time`),
    взятой — не меньше срока «долго в работе» от взятия (выключен — только по
    числу). У взятой напоминание к тому же не раньше взятия: поднятая и тут же
    взятая заявка не зовёт заново в момент взятия, а следующее напоминание — зовёт.
    """
    if raise_after is None or reminded_at is None:
        return False
    if is_claimed:
        taken_at = claimed_at or created_at
        if reminded_at < taken_at:
            return False
        overdue = stalled_delay is not None and reminded_at - taken_at >= stalled_delay
    else:
        overdue = step_by_time(reminded_at - created_at, delay) >= LadderStep.OVERDUE
    return reminder_count >= raise_after or overdue

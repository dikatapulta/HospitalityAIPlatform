"""Лестница невзятой заявки (spec 0045 §4): кого зовёт сигнал кабинета.

| Ступень | Когда | Кого зовёт |
|---|---|---|
| 1. Служба | с создания | сотрудников и старших службы заявки |
| 2. Просрочена | через `d` | их же: «Заявка #12 ждёт уже 30 мин» |
| 3. Ресепшен и менеджер | через `2d` | ещё и ресепшен с менеджером: «Никто не взял заявку #12» |

Ступень добавляет адресатов, а не заменяет: служба слышит свою заявку и на
третьей ступени. Сразу на третью идёт срочная (spec 0034), эскалация
(spec 0039, 0040) и заявка службы без единого активного сотрудника — звать
некого. `d` — `TenantConfig.reminder_delay_for(категория)`, тот же срок, что
у метки «просрочена» (P-12); выключенный срок (`None`) снимает ступени по
времени, три «сразу» остаются. Возраст — от создания заявки (spec 0042 §2.1).

Чистые функции без БД: числа на входе, ступень на выходе.
"""

from __future__ import annotations

import enum
from datetime import timedelta
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
) -> LadderStep:
    """Ступень невзятой заявки с учётом трёх «сразу на третью»."""
    if is_urgent or is_escalation or not service_staffed:
        return LadderStep.RECEPTION
    return step_by_time(age, delay)

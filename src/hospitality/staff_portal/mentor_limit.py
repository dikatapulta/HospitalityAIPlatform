"""Дневной лимит ходов «Наставника» на сотрудника (#966; число и расчёт — spec 0048 §8).

Потолок `LLM_MENTOR_DAILY_BUDGET_USD` — один на отель, а оборванный поток списывается
по полному резерву: без лимита один сотрудник циклом обрывов выбирал потолок, и
«Наставник» молчал у всего отеля до 00:00 UTC. Лимит — канон
`ratelimit.consume_rate_limit` по `user_id`, сутки UTC — те же, что у потолка. Выбран —
ERR-MENTOR-007 событием `error` до модели, текст сотруднику — как у потолка отеля.

`user_id` в логе отказа пишется, хотя маршруты «Наставника» его не пишут (Н-17,
`mentor_api.py`): у отвергнутого хода оценки нет, а по `user_id` находят источник при
ERR-OPS-019 ([alerts.md](../../../docs/runbooks/alerts.md#err-ops-019)).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Final

from hospitality.ai.mentor.common import MentorEvent
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_TURN_LIMIT: Final = "ERR-MENTOR-007"

# Ходов с моделью у сотрудника за UTC-сутки (spec 0048 §8).
MENTOR_DAILY_TURNS: Final = 200
_DAY_SECONDS: Final = 86_400


async def within_turn_limit(
    staff: StaffContext, events: AsyncGenerator[MentorEvent, None]
) -> AsyncGenerator[MentorEvent, None]:
    """Ход — в счёт дневного лимита сотрудника; выбран — ERR-MENTOR-007 до модели.

    Redis недоступен — ход идёт (fail-open канона `ratelimit.py`).
    """
    async with aclosing(events) as stream:
        decision = await consume_rate_limit(
            "mentor_turns",
            f"{staff.tenant_id}:{staff.user_id}",
            limit=MENTOR_DAILY_TURNS,
            window_seconds=_DAY_SECONDS,
        )
        if decision.available and not decision.allowed:
            logger.warning(
                "mentor_turn_limit_reached", user_id=str(staff.user_id), limit=MENTOR_DAILY_TURNS
            )
            raise AppError(
                code=ERR_MENTOR_TURN_LIMIT,
                message="Employee daily mentor turn limit reached",
                status_code=429,
            )
        async for event in stream:
            yield event

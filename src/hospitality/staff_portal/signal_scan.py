"""Невзятые заявки для сигнала кабинета — две выборки до среза (#971, находка О2-1 аудита 10.11).

Одной выборки «полсотни новейших» не хватало: 50 заявок вручную за полсекунду
вытесняли из неё срочную заявку гостя, и она переставала звать ресепшен и менеджера —
на вкладке и пушем (пуш строит тот же сигнал). Теперь выборок две, обе срезаны по
службам и этажам в SQL до `limit`:

- новейшие — их служба слышит с первой минуты (`list_unclaimed_requests`);
- с признаком, который зовёт шире службы (`list_flagged_unclaimed_requests`): срочные
  и эскалации первыми, затем напоминания гостя, заявки служб без сотрудников и заявки
  старше двух самых коротких сроков отеля — кандидаты на третью ступень лестницы.

Ступень каждой заявки по-прежнему решает лестница (`ladder.py`): выборка лишь
гарантирует, что кандидаты до неё дошли. Срочную и эскалацию форма кабинета не
создаёт, поэтому заливка заявок их не вытеснит. Упёрся срез — WARNING
`staff_signal_unclaimed_truncated` (образец `unclaimed_request_scan_truncated`):
полсотни невзятых — уже не очередь, и тихий срез выглядел бы как «всё в порядке».
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.shared.logging import get_logger
from hospitality.staff_portal.ladder import RECEPTION_DELAY_FACTOR

logger = get_logger(module=__name__)


async def scan_unclaimed(
    *,
    now: datetime,
    limit: int,
    config: TenantConfig | None,
    category_ids: Collection[uuid.UUID] | None,
    floor_scope: requests_api.FloorScope | None,
    unstaffed_category_ids: Collection[uuid.UUID] = (),
) -> list[requests_api.ServiceRequestRead]:
    """Новейшие невзятые и невзятые с признаком, без повторов, новые сверху.

    Граница возраста — `2d` по самому короткому сроку отеля (`min_reminder_delay`):
    заявка с более долгим сроком попадёт в выборку раньше своей ступени, а лишнее
    отбросит лестница. Срок выключен — по возрасту не берётся ничего.
    """
    newest = await requests_api.list_unclaimed_requests(
        created_before=now, limit=limit, category_ids=category_ids, floor_scope=floor_scope
    )
    delay = config.min_reminder_delay() if config is not None else None
    flagged = await requests_api.list_flagged_unclaimed_requests(
        created_before=now,
        limit=limit,
        created_at_or_before=None if delay is None else now - RECEPTION_DELAY_FACTOR * delay,
        flagged_category_ids=unstaffed_category_ids,
        category_ids=category_ids,
        floor_scope=floor_scope,
    )
    if len(newest) >= limit or len(flagged) >= limit:
        logger.warning(
            "staff_signal_unclaimed_truncated",
            limit=limit,
            newest=len(newest),
            flagged=len(flagged),
        )
    merged = {request.id: request for request in newest}
    for request in flagged:
        merged.setdefault(request.id, request)
    return sorted(merged.values(), key=lambda request: request.created_at, reverse=True)

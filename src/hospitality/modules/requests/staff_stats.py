"""Итоги по сотруднику за период — числа страницы «Статистика» (spec 0065 §2.2, issue #704).

Источник — журнал изменений ядра (`platform.staff_actions.list_request_events`),
а не колонки заявки: возврат в очередь стирает `claimed_by_*` (spec 0042 §2.1),
и «взял, а потом вернул» из заявки не восстановить. Заявка добавляет только
то, чего в журнале нет: момент создания (отсчёт ожидания, если заявку не
возвращали и не передавали) и последнее взятие (отсчёт «Выполняли»).

Считается в Python, как `day_summary`: выборка — строки одного отеля за период
не длиннее 30 дней. Отдельный файл по той же причине, что у `day_summary`, —
`service.py` и без него за границей R-3.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests.day_summary import _category_keys, _tenant_config
from hospitality.modules.requests.models import ServiceRequest
from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_actions import RequestEventRead, StaffAction, list_request_events
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Id заявок уходят в `IN (…)` порциями: у драйвера Postgres предел — 32 767
# параметров на запрос, а за 30 дней большого отеля заявок может быть больше.
_IDS_PER_QUERY = 5000

# Момент создания заявки и её последнего взятия (`service_requests`).
_Moments = tuple[datetime, datetime | None]


class StaffStatsRow(BaseModel):
    """Итоги одного сотрудника за период (spec 0065 §3).

    `login` — последний логин из журнала: страница показывает его вместо имени,
    когда учётку стёр суперадмин (spec 0044 §5). Медианы — `None`, когда мерить
    нечего (не брал, не выполнял); `overdue` — `None`, когда у отеля выключены
    напоминания: ноль читался бы как «сроки соблюдены» (канон `RequestsDaySummary`).
    """

    user_id: uuid.UUID
    login: str | None
    claimed: int
    done: int
    claim_wait_median_seconds: int | None
    work_median_seconds: int | None
    overdue: int | None
    returned: int


@dataclass
class _Totals:
    """Накопитель по одному сотруднику, пока идут строки журнала."""

    claimed: int = 0
    done: int = 0
    overdue: int = 0
    returned: int = 0
    waits: list[timedelta] = field(default_factory=list)
    works: list[timedelta] = field(default_factory=list)
    login: str | None = None
    login_at: datetime | None = None


async def staff_stats(
    *, since: datetime, until: datetime, category_ids: Collection[uuid.UUID] | None
) -> list[StaffStatsRow]:
    """Итоги каждого сотрудника, у которого в `[since, until)` есть взятие или
    «Готово» в службах `category_ids` (None — весь отель), по `user_id`.

    Граница старшего (spec 0065 §2.3) режет только сами взятия и «Готово»:
    возврат и передача той же заявки нужны для отсчёта и границей не режутся.
    Строки без личности (учётка удалена) в итоги не идут. Вызывается внутри
    `tenant_context` — журнал и заявки читаются под RLS текущего тенанта (P-4).
    """
    scope = frozenset(category_ids) if category_ids is not None else None
    events = await list_request_events(since=since, until=until, category_ids=scope)
    async with session_scope() as session:
        config = await _tenant_config(session)
        category_keys = await _category_keys(session)
        moments = await _request_moments(session, {event.request_id for event in events})

    totals: dict[uuid.UUID, _Totals] = {}
    reminders = config if config is not None and config.min_reminder_delay() is not None else None
    for request_id, group in groupby(events, key=lambda event: event.request_id):
        _add_request(
            list(group),
            moments.get(request_id),
            totals,
            counts=lambda event: _counts(event, since=since, until=until, scope=scope),
            overdue_after=lambda event: _deadline(event, category_keys, reminders),
        )
    rows = [
        StaffStatsRow(
            user_id=user_id,
            login=item.login,
            claimed=item.claimed,
            done=item.done,
            claim_wait_median_seconds=_median_seconds(item.waits),
            work_median_seconds=_median_seconds(item.works),
            overdue=item.overdue if reminders is not None else None,
            returned=item.returned,
        )
        for user_id, item in totals.items()
    ]
    rows.sort(key=lambda row: str(row.user_id))
    logger.info("staff_stats_built", staff=len(rows), requests=len(moments))
    return rows


def _add_request(
    events: Sequence[RequestEventRead],
    moments: _Moments | None,
    totals: dict[uuid.UUID, _Totals],
    *,
    counts: Callable[[RequestEventRead], bool],
    overdue_after: Callable[[RequestEventRead], timedelta | None],
) -> None:
    """Строки одной заявки по порядку: возврат и передача сдвигают момент прихода
    в службу, засчитанные взятия и «Готово» ложатся в итоги своего сотрудника.

    Возврат засчитывается тому, кто брал, а не тому, кто вернул: следующая строка
    заявки после его взятия — `request_returned`. Передача — не возврат: «не та
    служба», а не «не сделал». Следующая строка — снова взятие (вернули из
    Telegram, журнал этого не пишет) — исход неизвестен, возвратом не считается.
    """
    created_at, claimed_at = moments if moments is not None else (None, None)
    arrived = created_at
    for index, event in enumerate(events):
        if event.action in (StaffAction.REQUEST_RETURNED, StaffAction.REQUEST_TRANSFERRED):
            arrived = event.occurred_at
            continue
        if event.actor_user_id is None or not counts(event):
            continue
        following = events[index + 1] if index + 1 < len(events) else None
        if event.action is StaffAction.REQUEST_DONE and _reopened(following):
            continue  # закрыли по ошибке и вернули (spec 0080 §2.4) — не «Готово»
        item = totals.setdefault(event.actor_user_id, _Totals())
        if event.actor_login and (item.login_at is None or event.occurred_at > item.login_at):
            item.login, item.login_at = event.actor_login, event.occurred_at
        if event.action is StaffAction.REQUEST_DONE:
            item.done += 1
            if claimed_at is not None:
                item.works.append(event.occurred_at - claimed_at)
            continue
        item.claimed += 1
        if following is not None and following.action is StaffAction.REQUEST_RETURNED:
            item.returned += 1
        if arrived is None:
            continue
        waited = event.occurred_at - arrived
        item.waits.append(waited)
        deadline = overdue_after(event)
        if deadline is not None and waited >= deadline:
            item.overdue += 1


def _reopened(event: RequestEventRead | None) -> bool:
    return event is not None and event.action is StaffAction.REQUEST_REOPENED


def _counts(
    event: RequestEventRead, *, since: datetime, until: datetime, scope: frozenset[uuid.UUID] | None
) -> bool:
    """Взятие или «Готово» в периоде и в службах читающего (spec 0065 §2.3)."""
    if event.action not in (StaffAction.REQUEST_CLAIMED, StaffAction.REQUEST_DONE):
        return False
    if not since <= event.occurred_at < until:
        return False
    return scope is None or event.category_id in scope


def _deadline(
    event: RequestEventRead, category_keys: dict[uuid.UUID, str], config: TenantConfig | None
) -> timedelta | None:
    """Срок службы СТРОКИ взятия, а не текущей службы заявки: переданную после
    взятия заявку брали в одной службе, а сейчас она в другой. `config` None —
    напоминания у отеля выключены, просрочки нет как явления."""
    if config is None:
        return None
    key = category_keys.get(event.category_id) if event.category_id is not None else None
    return config.reminder_delay_for(key)


def _median_seconds(durations: Iterable[timedelta]) -> int | None:
    """Медиана в секундах; `None` — мерить нечего (довод медианы — spec 0035 §6)."""
    values = [duration.total_seconds() for duration in durations]
    if not values:
        return None
    return int(statistics.median(values))


async def _request_moments(
    session: AsyncSession, request_ids: set[uuid.UUID]
) -> dict[uuid.UUID, _Moments]:
    """`created_at` и `claimed_at` заявок; удалённой заявки в ответе нет — её
    взятия считаются, а время ожидания у них не меряется."""
    ids = sorted(request_ids, key=str)
    moments: dict[uuid.UUID, _Moments] = {}
    for start in range(0, len(ids), _IDS_PER_QUERY):
        rows = await session.execute(
            select(ServiceRequest.id, ServiceRequest.created_at, ServiceRequest.claimed_at).where(
                ServiceRequest.id.in_(ids[start : start + _IDS_PER_QUERY])
            )
        )
        for request_id, created_at, claimed_at in rows:
            moments[request_id] = (created_at, claimed_at)
    return moments

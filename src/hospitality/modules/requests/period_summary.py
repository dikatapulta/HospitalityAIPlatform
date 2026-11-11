"""Числа заявок за период отеля — лист «Отчёт» менеджера (spec 0083 §2.3, issue #950).

Сосед `day_summary.py` и копия его правила: каждое число считает владелец данных,
лист их только раскладывает (spec 0035 §6, P-5). Отличие одно — окно: не сутки, а
дни `[first_day, last_day]` отеля. Предикат просрочки, границы суток, срез служб и
конфиг берутся из `day_summary` — второй копии предиката нет, отсечка просрочки —
конец периода.

Среза по службам нет: лист видит только менеджер (§2.5).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo

from pydantic import BaseModel
from sqlalchemy import Row, select

from hospitality.modules.requests.day_summary import (
    _category_keys,
    _claim_median_seconds,
    _CreatedRow,
    _local_midnight_utc,
    _overdue_count,
    _tenant_config,
)
from hospitality.modules.requests.models import RequestStatus, ServiceRequest
from hospitality.platform.config import TenantConfig
from hospitality.shared.db import session_scope

_WaitRow = Row[uuid.UUID, datetime, datetime | None]
_ClosedRow = Row[uuid.UUID, RequestStatus, datetime, datetime | None]


class PeriodCounts(BaseModel):
    """Числа заявок за период — у отеля целиком или у одной службы.

    `None` — числа нет, а не ноль (канон `RequestsDaySummary`): медиану мерить
    нечем — в периоде не брали или не выполняли; просрочки нет как явления —
    у отеля выключены напоминания.
    """

    created: int = 0
    done: int = 0
    cancelled: int = 0
    overdue: int | None = None
    claim_median_seconds: int | None = None
    done_median_seconds: int | None = None


class ServicePeriodCounts(PeriodCounts):
    """Строка «По службам»: категория — идентификатором, имя отдаёт `list_categories()`."""

    category_id: uuid.UUID


class RequestsPeriodSummary(BaseModel):
    """Числа заявок за дни `[first_day, last_day]` отеля.

    `period_start` / `period_end` — окно в UTC: по нему остальные владельцы чисел
    листа (оценки, идеи, вопросы без ответа) считают свои числа за ровно это же
    окно, а не за своё (довод — докстринг `RequestsDaySummary`). `by_service` —
    службы, у которых в окне что-то создано или закрыто; порядок задаёт лист.
    """

    first_day: date
    last_day: date
    period_start: datetime
    period_end: datetime
    total: PeriodCounts
    by_service: list[ServicePeriodCounts]


async def period_summary(first_day: date, last_day: date) -> RequestsPeriodSummary:
    """Собрать числа заявок тенанта за дни отеля (внутри `tenant_context`).

    Три выборки за окно, как у `day_summary`: созданные (по `service_day`), взятые
    (по `claimed_at`) и закрытые (по `closed_at`). Среза (`limit`) нет — урезанная
    выборка дала бы неверное число; окно ограничивает лист (до 92 дней, §2.2).
    Тенант без конфига — UTC и без просрочки (канон `day_summary`).
    """
    async with session_scope() as session:
        config = await _tenant_config(session)
        zone: tzinfo = config.tzinfo if config is not None else UTC
        period_start = _local_midnight_utc(first_day, zone)
        period_end = _local_midnight_utc(last_day + timedelta(days=1), zone)
        created_rows: Sequence[_CreatedRow] = (
            await session.execute(
                select(
                    ServiceRequest.category_id,
                    ServiceRequest.origin,
                    ServiceRequest.status,
                    ServiceRequest.created_at,
                    ServiceRequest.claimed_at,
                    ServiceRequest.closed_at,
                ).where(
                    ServiceRequest.service_day >= first_day,
                    ServiceRequest.service_day <= last_day,
                )
            )
        ).all()
        claimed_rows: Sequence[_WaitRow] = (
            await session.execute(
                select(
                    ServiceRequest.category_id, ServiceRequest.created_at, ServiceRequest.claimed_at
                ).where(
                    ServiceRequest.claimed_at >= period_start,
                    ServiceRequest.claimed_at < period_end,
                )
            )
        ).all()
        closed_rows: Sequence[_ClosedRow] = (
            await session.execute(
                select(
                    ServiceRequest.category_id,
                    ServiceRequest.status,
                    ServiceRequest.created_at,
                    ServiceRequest.closed_at,
                ).where(
                    ServiceRequest.status.in_((RequestStatus.DONE, RequestStatus.CANCELLED)),
                    ServiceRequest.closed_at >= period_start,
                    ServiceRequest.closed_at < period_end,
                )
            )
        ).all()
        category_keys = await _category_keys(session)

    def counts(category_id: uuid.UUID | None) -> PeriodCounts:
        def mine(row_category: uuid.UUID) -> bool:
            return category_id is None or row_category == category_id

        return _counts(
            [row for row in created_rows if mine(row.category_id)],
            [row for row in claimed_rows if mine(row.category_id)],
            [row for row in closed_rows if mine(row.category_id)],
            category_keys,
            config,
            period_end=period_end,
        )

    active = {row.category_id for row in (*created_rows, *claimed_rows, *closed_rows)}
    return RequestsPeriodSummary(
        first_day=first_day,
        last_day=last_day,
        period_start=period_start,
        period_end=period_end,
        total=counts(None),
        by_service=[
            ServicePeriodCounts(category_id=category_id, **counts(category_id).model_dump())
            for category_id in sorted(active, key=str)
        ],
    )


def _counts(
    created_rows: Sequence[_CreatedRow],
    claimed_rows: Sequence[_WaitRow],
    closed_rows: Sequence[_ClosedRow],
    category_keys: dict[uuid.UUID, str],
    config: TenantConfig | None,
    *,
    period_end: datetime,
) -> PeriodCounts:
    """Числа одного среза. «Взяли за» — от заявки до «Взять» по взятым в окне,
    «Выполнили за» — от заявки до «Готово» по выполненным в окне (§2.3); медиана
    обоих — `_claim_median_seconds` сводки дня, она про любые длительности."""
    by_status: defaultdict[RequestStatus, int] = defaultdict(int)
    done_waits: list[timedelta] = []
    for row in closed_rows:
        by_status[row.status] += 1
        if row.status is RequestStatus.DONE and row.closed_at is not None:
            done_waits.append(row.closed_at - row.created_at)
    return PeriodCounts(
        created=len(created_rows),
        done=by_status[RequestStatus.DONE],
        cancelled=by_status[RequestStatus.CANCELLED],
        overdue=_overdue_count(created_rows, category_keys, config, day_end=period_end),
        claim_median_seconds=_claim_median_seconds(
            [row.claimed_at - row.created_at for row in claimed_rows if row.claimed_at is not None]
        ),
        done_median_seconds=_claim_median_seconds(done_waits),
    )

"""Невзятые заявки с признаком, который зовёт шире службы (#971, находка О2-1 аудита 10.11).

Сигнал кабинета берёт полсотни новейших невзятых (`service.list_unclaimed_requests`),
и 50 заявок вручную за полсекунду вытесняли из среза срочную заявку гостя: она
переставала звать ресепшен и менеджера. Эта выборка — вторая, рядом с той: невзятые
с признаком, отобранные в SQL до среза `limit`, срочные и эскалации первыми.

Отдельный файл, а не функция в `service.py`: тот за границей R-3 — довод
`reminders.py`. Вход снаружи — реэкспорт через `api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import case, or_, select
from sqlalchemy.sql.elements import ColumnElement

from hospitality.modules.requests.models import RequestStatus, ServiceRequest, ServiceRequestOrigin
from hospitality.modules.requests.schemas import FloorScope, ServiceRequestRead
from hospitality.modules.requests.service import _list_scoped


async def list_flagged_unclaimed_requests(
    *,
    created_before: datetime,
    limit: int,
    created_at_or_before: datetime | None = None,
    flagged_category_ids: Collection[uuid.UUID] = (),
    category_ids: Collection[uuid.UUID] | None = None,
    floor_scope: FloorScope | None = None,
) -> list[ServiceRequestRead]:
    """Невзятые (`new`, созданы раньше `created_before`) с одним из признаков: срочная,
    эскалация, гость напоминал, создана не позже `created_at_or_before` или в службе
    из `flagged_category_ids`.

    Порядок — срочные и эскалации первыми, затем остальные, внутри — новые сверху.
    Срочную и эскалацию персонал вручную не создаёт (форма кабинета пишет
    `is_urgent=False` и `origin=staff_manual`), поэтому заливка заявок их не вытеснит.
    Модуль не знает ни сроков, ни лестницы: границу возраста и службы без
    сотрудников передаёт вызывающая сторона. `category_ids` и `floor_scope` — те же
    срезы по службам и этажам, что у `list_unclaimed_requests`, тоже до `limit`.
    """
    loud = _loud()
    flags: list[ColumnElement[bool]] = [loud, ServiceRequest.guest_reminded_at.is_not(None)]
    if created_at_or_before is not None:
        flags.append(ServiceRequest.created_at <= created_at_or_before)
    if flagged_category_ids:
        flags.append(ServiceRequest.category_id.in_(list(flagged_category_ids)))
    query = select(ServiceRequest).where(
        ServiceRequest.status == RequestStatus.NEW,
        ServiceRequest.created_at < created_before,
        or_(*flags),
    )
    return await _list_scoped(
        query.order_by(
            case((loud, 0), else_=1), ServiceRequest.created_at.desc(), ServiceRequest.id
        ).limit(limit),
        category_ids=category_ids,
        floor_scope=floor_scope,
    )


def _loud() -> ColumnElement[bool]:
    """Срочная или эскалация — сразу на третью ступень лестницы кабинета."""
    return or_(
        ServiceRequest.is_urgent.is_(True),
        ServiceRequest.origin == ServiceRequestOrigin.ESCALATION,
    )

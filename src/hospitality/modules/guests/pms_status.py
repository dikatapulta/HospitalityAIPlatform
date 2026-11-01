"""Состояние выгрузки Opera для кабинета (spec 0058 §2.5–§2.6).

Читает то же, что сверка: последний снимок — из порта PMS, придержанные
выезды и расхождения последнего применённого снимка — из `pms_sync_state`.
Порог «устарел» один на алерт (`pms_sync.py`) и на полосу кабинета: три
интервала выгрузки отеля.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from hospitality.modules.guests.models import PmsSyncState, StaySource
from hospitality.modules.guests.schemas import PmsDiscrepancy, PmsStatus, StayRead
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.ports.pms import get_pms
from hospitality.shared.tenancy import current_tenant_id

# Снимок «устарел» после стольких интервалов выгрузки тенанта (§2.6).
STALE_INTERVALS: Final = 3


def pms_stale_after(interval_minutes: int) -> timedelta:
    """Возраст снимка, после которого он устарел: три интервала выгрузки."""
    return timedelta(minutes=STALE_INTERVALS * interval_minutes)


async def get_pms_status(*, interval_minutes: int) -> PmsStatus:
    """Полоса, расхождения и строки карточек кабинета — текущего тенанта.

    Зовёт кабинет только у тенанта с `pms_source: opera_report`;
    `interval_minutes` — `TenantConfig.pms_snapshot_interval_minutes`.
    """
    tenant_id = current_tenant_id()
    snapshot = await get_pms().fetch_snapshot(tenant_id)
    async with session_scope() as session:
        state = await session.get(PmsSyncState, tenant_id)
    taken_at = snapshot.taken_at if snapshot is not None else None
    return PmsStatus(
        interval_minutes=interval_minutes,
        taken_at=taken_at,
        stale=taken_at is not None and utc_now() - taken_at > pms_stale_after(interval_minutes),
        held_closures=state.held_closures if state is not None else 0,
        discrepancies=(
            [PmsDiscrepancy.model_validate(item) for item in state.discrepancies]
            if state is not None
            else []
        ),
    )


def missing_from_pms(stay: StayRead, status: PmsStatus) -> bool:
    """Ручной Stay, которого Opera не знает: снимок взят через три интервала после заезда.

    Ресепшену хватило времени ввести гостя в Opera, а выгрузка его так и не
    связала — значит, в Opera его нет или комната другая (§2.5).
    """
    return (
        stay.source is StaySource.MANUAL
        and status.taken_at is not None
        and status.taken_at - stay.check_in_at > pms_stale_after(status.interval_minutes)
    )

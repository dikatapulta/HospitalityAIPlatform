"""«Срочно к часу» (spec 0062 §2, §8 PR 2): метка на открытом пути, снятие, закрытый
путь, прошедший срок, изоляция тенантов."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hospitality.modules.housekeeping.api import (
    ERR_HOUSEKEEPING_DEADLINE_PASSED,
    ERR_HOUSEKEEPING_INVALID_TRANSITION,
    ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND,
    TurnoverStatus,
    change_turnover_status,
    get_turnover,
    list_open_turnovers,
    report_checkout,
    set_turnover_deadline,
)
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context

Tenants = tuple[uuid.UUID, uuid.UUID]


async def _outbox_count() -> int:
    async with platform_session_scope() as session:
        return int(await session.scalar(select(func.count(OutboxEvent.id))) or 0)


_OPEN_PATHS: tuple[list[TurnoverStatus], ...] = (
    [],
    [TurnoverStatus.INSPECTED],
    [TurnoverStatus.INSPECTED, TurnoverStatus.CLEANING],
    [TurnoverStatus.INSPECTED, TurnoverStatus.CLEANING, TurnoverStatus.CLEANED],
)


@pytest.mark.parametrize("path", _OPEN_PATHS)
async def test_deadline_is_set_and_cleared_on_every_open_step(
    two_tenants: Tenants, path: list[TurnoverStatus]
) -> None:
    tenant_a, _ = two_tenants
    needed_by = utc_now() + timedelta(hours=2)
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
        for status in path:
            await change_turnover_status(turnover_id, status)
        events_before = await _outbox_count()
        marked = await set_turnover_deadline(turnover_id, needed_by)
        assert marked.needed_by_at == needed_by
        assert (await get_turnover(turnover_id)).needed_by_at == needed_by
        assert [row.needed_by_at for row in await list_open_turnovers(limit=10)] == [needed_by]
        moved = await set_turnover_deadline(turnover_id, needed_by + timedelta(minutes=30))
        assert moved.needed_by_at == needed_by + timedelta(minutes=30)
        cleared = await set_turnover_deadline(turnover_id, None)
        assert cleared.needed_by_at is None
        assert cleared.status is (path[-1] if path else TurnoverStatus.REPORTED)
    # Метка — работа кабинета: событий для синхронизации с OPERA нет (NG-2).
    assert await _outbox_count() == events_before


@pytest.mark.parametrize("closing", [TurnoverStatus.CANCELLED, TurnoverStatus.READY])
async def test_closed_path_cannot_be_marked(two_tenants: Tenants, closing: TurnoverStatus) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
        if closing is TurnoverStatus.READY:
            for status in (
                TurnoverStatus.INSPECTED,
                TurnoverStatus.CLEANING,
                TurnoverStatus.CLEANED,
            ):
                await change_turnover_status(turnover_id, status)
        await change_turnover_status(turnover_id, closing)
        for needed_by in (utc_now() + timedelta(hours=1), None):
            with pytest.raises(AppError) as error:
                await set_turnover_deadline(turnover_id, needed_by)
            assert error.value.code == ERR_HOUSEKEEPING_INVALID_TRANSITION
            assert error.value.status_code == 409


async def test_passed_moment_is_rejected_and_mark_stays(two_tenants: Tenants) -> None:
    tenant_a, _ = two_tenants
    needed_by = utc_now() + timedelta(hours=1)
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
        await set_turnover_deadline(turnover_id, needed_by)
        for passed in (utc_now() - timedelta(minutes=1), utc_now() - timedelta(days=1)):
            with pytest.raises(AppError) as error:
                await set_turnover_deadline(turnover_id, passed)
            assert error.value.code == ERR_HOUSEKEEPING_DEADLINE_PASSED
            assert error.value.status_code == 422
        assert (await get_turnover(turnover_id)).needed_by_at == needed_by


async def test_deadline_is_isolated_between_tenants(two_tenants: Tenants) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
    with tenant_context(tenant_b):
        with pytest.raises(AppError) as error:
            await set_turnover_deadline(turnover_id, utc_now() + timedelta(hours=1))
        assert error.value.code == ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND
    with tenant_context(tenant_a):
        assert (await get_turnover(turnover_id)).needed_by_at is None

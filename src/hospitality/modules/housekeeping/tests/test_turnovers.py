"""Путь номера после выезда (spec 0046 §3, §8, §11): шаги, порядок, один открытый
путь на номер, изоляция тенантов, «сегодня убрано» и ретеншн замечаний."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from hospitality.modules.housekeeping import service
from hospitality.modules.housekeeping.api import (
    ERR_HOUSEKEEPING_INVALID_TRANSITION,
    ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND,
    TurnoverStatus,
    change_turnover_status,
    clear_expired_inspection_notes,
    count_cleaned_since,
    get_turnover,
    list_open_turnovers,
    list_turnovers_ready_since,
    report_checkout,
)
from hospitality.modules.housekeeping.models import RoomTurnover
from hospitality.modules.housekeeping.tests.conftest import make_acting_user
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context
from tests.test_tenant_config import _wait_until_a_backend_blocks

Tenants = tuple[uuid.UUID, uuid.UUID]


async def _outbox_names() -> list[str]:
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(OutboxEvent.event_name).order_by(OutboxEvent.occurred_at)
        )
    return list(rows)


async def _shift(turnover_id: uuid.UUID, **columns: Any) -> None:
    """Передвинуть метки времени прямо в БД (канон test_retention): ждать сутки нельзя."""
    async with session_scope() as session:
        await session.execute(
            update(RoomTurnover).where(RoomTurnover.id == turnover_id).values(**columns)
        )


async def test_full_path_stamps_each_step_and_publishes_events(two_tenants: Tenants) -> None:
    tenant_a, _ = two_tenants
    reception = await make_acting_user("Алия")
    maid = await make_acting_user("Айгуль")
    senior = await make_acting_user("Гульнара")
    with tenant_context(tenant_a):
        report = await report_checkout(" 704 ", acting_user=reception)
        turnover_id = report.turnover.id
        assert report.created is True
        assert report.turnover.room_number == "704"
        assert report.turnover.status is TurnoverStatus.REPORTED
        assert report.turnover.reported_by_display_name == "Алия"

        inspected = await change_turnover_status(
            turnover_id,
            TurnoverStatus.INSPECTED,
            acting_user=maid,
            inspection_note="  Минибар: 2 колы  ",
        )
        assert inspected.inspection_note == "Минибар: 2 колы"
        assert inspected.inspected_by_display_name == "Айгуль"
        await change_turnover_status(turnover_id, TurnoverStatus.CLEANING, acting_user=maid)
        await change_turnover_status(turnover_id, TurnoverStatus.CLEANED, acting_user=maid)
        ready = await change_turnover_status(turnover_id, TurnoverStatus.READY, acting_user=senior)

    assert ready.status is TurnoverStatus.READY
    assert ready.cleaning_started_by_display_name == "Айгуль"
    assert ready.cleaned_by_display_name == "Айгуль"
    assert ready.accepted_by_display_name == "Гульнара"
    assert ready.inspected_at is not None and ready.ready_at is not None
    assert ready.inspected_at <= ready.ready_at
    names = await _outbox_names()
    assert names == ["room_turnover.reported"] + ["room_turnover.status_changed"] * 4


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        ([], TurnoverStatus.CLEANING),  # «Убираю» без проверки
        ([], TurnoverStatus.READY),  # приёмка без уборки
        ([TurnoverStatus.INSPECTED], TurnoverStatus.INSPECTED),  # второе «Проверено»
        ([TurnoverStatus.CANCELLED], TurnoverStatus.INSPECTED),  # шаг после отмены
        (
            [
                TurnoverStatus.INSPECTED,
                TurnoverStatus.CLEANING,
                TurnoverStatus.CLEANED,
                TurnoverStatus.READY,
            ],
            TurnoverStatus.CANCELLED,  # отмена принятого
        ),
    ],
)
async def test_out_of_order_step_is_rejected(
    two_tenants: Tenants, path: list[TurnoverStatus], forbidden: TurnoverStatus
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
        for status in path:
            await change_turnover_status(turnover_id, status)
        with pytest.raises(AppError) as error:
            await change_turnover_status(turnover_id, forbidden)
        assert error.value.code == ERR_HOUSEKEEPING_INVALID_TRANSITION
        assert error.value.status_code == 409


async def test_note_is_written_only_on_inspection(two_tenants: Tenants) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
        inspected = await change_turnover_status(
            turnover_id, TurnoverStatus.INSPECTED, inspection_note="   "
        )
        assert inspected.inspection_note is None
        cleaning = await change_turnover_status(
            turnover_id, TurnoverStatus.CLEANING, inspection_note="поздно"
        )
    assert cleaning.inspection_note is None
    # Шаг без личности: время есть, имени нет (канон spec 0035 §3).
    assert cleaning.cleaning_started_at is not None
    assert cleaning.cleaning_started_by_display_name is None


async def test_repeated_report_returns_open_path_and_new_one_after_close(
    two_tenants: Tenants,
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        first = await report_checkout("704")
        again = await report_checkout("704")
        assert again.created is False
        assert again.turnover.id == first.turnover.id

        await change_turnover_status(first.turnover.id, TurnoverStatus.CANCELLED)
        after_cancel = await report_checkout("704")
        assert after_cancel.created is True
        assert after_cancel.turnover.id != first.turnover.id
        other_room = await report_checkout("705")
        assert other_room.created is True
    assert (
        await _outbox_names()
        == ["room_turnover.reported", "room_turnover.status_changed"]
        + ["room_turnover.reported"] * 2
    )


async def test_repeated_report_within_window_is_the_same_checkout(two_tenants: Tenants) -> None:
    """Второй ресепшенист через несколько часов — тот же выезд (spec 0046 §3)."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        first = await report_checkout("704")
        await change_turnover_status(first.turnover.id, TurnoverStatus.INSPECTED)
        inside = utc_now() - service.DUPLICATE_REPORT_WINDOW + timedelta(minutes=5)
        await _shift(first.turnover.id, reported_at=inside)
        again = await report_checkout("704")
        open_paths = await list_open_turnovers(limit=10)
    assert again.created is False
    assert again.turnover.id == first.turnover.id
    assert [(path.id, path.status) for path in open_paths] == [
        (first.turnover.id, TurnoverStatus.INSPECTED)
    ]


@pytest.mark.parametrize("left_at", [TurnoverStatus.REPORTED, TurnoverStatus.CLEANED])
async def test_stale_open_path_does_not_swallow_next_checkout(
    two_tenants: Tenants, left_at: TurnoverStatus
) -> None:
    """Вчера убрали, а «Принять» никто не нажал: сегодняшний выезд того же номера —
    новый путь в «Ждут проверки», забытый закрыт отменой без «кто» (Б-1 ревью #481)."""
    tenant_a, _ = two_tenants
    reception = await make_acting_user("Алия")
    with tenant_context(tenant_a):
        stale_id = (await report_checkout("705")).turnover.id
        if left_at is TurnoverStatus.CLEANED:
            for status in (
                TurnoverStatus.INSPECTED,
                TurnoverStatus.CLEANING,
                TurnoverStatus.CLEANED,
            ):
                await change_turnover_status(stale_id, status)
        outside = utc_now() - service.DUPLICATE_REPORT_WINDOW - timedelta(minutes=5)
        await _shift(stale_id, reported_at=outside)
        events_before = len(await _outbox_names())

        fresh = await report_checkout("705", acting_user=reception)
        stale = await get_turnover(stale_id)
        open_paths = await list_open_turnovers(limit=10)

    assert fresh.created is True
    assert fresh.turnover.id != stale_id
    assert fresh.turnover.status is TurnoverStatus.REPORTED
    assert fresh.turnover.reported_by_display_name == "Алия"
    assert stale.status is TurnoverStatus.CANCELLED
    assert stale.cancelled_at is not None
    assert stale.cancelled_by_display_name is None
    assert [path.id for path in open_paths] == [fresh.turnover.id]
    assert (await _outbox_names())[events_before:] == [
        "room_turnover.status_changed",
        "room_turnover.reported",
    ]


async def test_stale_close_does_not_overwrite_a_concurrent_accept(two_tenants: Tenants) -> None:
    """Старшая жмёт «Принять» на вчерашнем пути в тот миг, когда ресепшен отмечает
    новый выезд: приёмка остаётся приёмкой. Чередование задано — отметка ждёт
    блокировку строки; без `FOR UPDATE` она перечитала бы старый `cleaned` и
    записала `cancelled` поверх `ready`."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        stale_id = (await report_checkout("705")).turnover.id
        for status in (TurnoverStatus.INSPECTED, TurnoverStatus.CLEANING, TurnoverStatus.CLEANED):
            await change_turnover_status(stale_id, status)
        await _shift(stale_id, reported_at=utc_now() - timedelta(days=1))

        async with session_scope() as accepting:
            await accepting.execute(
                update(RoomTurnover)
                .where(RoomTurnover.id == stale_id)
                .values(status=TurnoverStatus.READY, ready_at=utc_now())
            )
            reporting = asyncio.create_task(report_checkout("705"))
            blocked = await _wait_until_a_backend_blocks()
        fresh = await reporting
        accepted = await get_turnover(stale_id)

    assert blocked, "отметка не встала на блокировку — чередования не было"
    assert accepted.status is TurnoverStatus.READY
    assert accepted.cancelled_at is None
    assert fresh.created is True


async def test_race_of_two_reports_returns_winner(
    two_tenants: Tenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка «уже открыт» проиграла гонку: вставку отвергает partial UNIQUE,
    и проигравший получает путь победителя, а не 500."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        winner = await report_checkout("704")
        real_find_open = service._find_open
        calls = {"count": 0}

        async def find_open_losing_race(*args: Any, **kwargs: Any) -> RoomTurnover | None:
            calls["count"] += 1
            if calls["count"] == 1:
                return None  # пока проверял, второй ресепшенист успел вставить
            return await real_find_open(*args, **kwargs)

        monkeypatch.setattr(service, "_find_open", find_open_losing_race)
        loser = await report_checkout("704")
        open_paths = await list_open_turnovers(limit=10)
    assert loser.created is False
    assert loser.turnover.id == winner.turnover.id
    assert [path.id for path in open_paths] == [winner.turnover.id]


async def test_paths_are_isolated_between_tenants(two_tenants: Tenants) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        turnover_id = (await report_checkout("704")).turnover.id
    with tenant_context(tenant_b):
        assert await list_open_turnovers(limit=10) == []
        # Тот же номер у другого отеля — свой путь, а не «уже открыт».
        assert (await report_checkout("704")).created is True
        for call in (
            get_turnover(turnover_id),
            change_turnover_status(turnover_id, TurnoverStatus.INSPECTED),
        ):
            with pytest.raises(AppError) as error:
                await call
            assert error.value.code == ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND
            assert error.value.status_code == 404


async def test_lists_and_cleaned_counts_respect_boundaries(two_tenants: Tenants) -> None:
    tenant_a, _ = two_tenants
    maid = await make_acting_user("Айгуль")
    other = await make_acting_user("Дина")
    now = utc_now()
    midnight = now - timedelta(hours=1)
    with tenant_context(tenant_a):
        ids = {}
        for room in ("701", "702", "703", "704", "705"):
            ids[room] = (await report_checkout(room)).turnover.id
        for room, actor in (("701", maid), ("702", maid), ("703", other), ("704", maid)):
            for status in (TurnoverStatus.INSPECTED, TurnoverStatus.CLEANING):
                await change_turnover_status(ids[room], status, acting_user=actor)
            await change_turnover_status(ids[room], TurnoverStatus.CLEANED, acting_user=actor)
        await change_turnover_status(ids["701"], TurnoverStatus.READY)
        await change_turnover_status(ids["702"], TurnoverStatus.READY)
        # 702 убрали и приняли вчера; 704 — убрали и отменили (ошибочный выезд).
        await _shift(ids["702"], cleaned_at=midnight - timedelta(hours=2))
        await _shift(ids["702"], ready_at=midnight - timedelta(hours=1))
        await change_turnover_status(ids["704"], TurnoverStatus.CANCELLED)
        await _shift(ids["705"], reported_at=now - timedelta(hours=5))

        open_rooms = [path.room_number for path in await list_open_turnovers(limit=10)]
        ready_rooms = [
            path.room_number for path in await list_turnovers_ready_since(midnight, limit=10)
        ]
        cleaned = await count_cleaned_since(midnight)

    assert open_rooms == ["705", "703"]  # давние сверху, закрытые и отменённые — нет
    assert ready_rooms == ["701"]
    assert [(row.display_name, row.count) for row in cleaned] == [("Айгуль", 1), ("Дина", 1)]


async def test_retention_clears_only_old_notes(two_tenants: Tenants) -> None:
    tenant_a, _ = two_tenants
    cutoff = utc_now() - timedelta(days=90)
    with tenant_context(tenant_a):
        old_id = (await report_checkout("701")).turnover.id
        fresh_id = (await report_checkout("702")).turnover.id
        for turnover_id in (old_id, fresh_id):
            await change_turnover_status(
                turnover_id, TurnoverStatus.INSPECTED, inspection_note="Гость оставил зарядку"
            )
        await _shift(old_id, created_at=cutoff - timedelta(days=1))

        assert await clear_expired_inspection_notes(created_before=cutoff) == 1
        assert await clear_expired_inspection_notes(created_before=cutoff) == 0
        old = await get_turnover(old_id)
        fresh = await get_turnover(fresh_id)
    assert old.inspection_note is None
    assert old.status is TurnoverStatus.INSPECTED
    assert old.inspected_at is not None
    assert fresh.inspection_note == "Гость оставил зарядку"

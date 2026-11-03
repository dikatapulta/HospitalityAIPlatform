"""Итоги по сотруднику (spec 0065 §6 пп. 1–3, 5, 7–9; issue #704).

Сценарии строятся строками журнала с заданным моментом (канон `_seed` из
`staff_portal/tests/test_changes.py`) и сдвигом меток заявки (`shift_request`):
ждать минуты нельзя. Один сценарий идёт боевым путём — переходами статуса
с личностью, — чтобы держать связь «что пишет журнал» ↔ «что считает страница».
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    StaffStatsRow,
    change_request_status,
    create_request,
    staff_stats,
)
from hospitality.modules.requests.staff_stats import _median_seconds
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    shift_request,
    store_hotel_config,
)
from hospitality.platform.models import StaffActionLog
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    StaffActor,
    record_automatic_action,
    record_staff_action,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

# Полдень отеля (Алматы, UTC+5) 20.10 — далеко от границ суток.
T0 = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
SINCE = T0 - timedelta(days=1)
UNTIL = T0 + timedelta(days=1)
MIN = timedelta(minutes=1)


async def _actor(name: str, login: str) -> StaffActor:
    acting = await make_acting_user(name)
    return StaffActor(user_id=acting.user_id, display_name=name, login=login)


async def _request(category_id: uuid.UUID, *, created_at: datetime = T0) -> uuid.UUID:
    """Заявка гостя (без строки журнала) с заданным моментом создания."""
    created = await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=ServiceRequestOrigin.GUEST_CHAT,
            summary="guest request",
            room_number="305",
        )
    )
    await shift_request(created.id, created_at=created_at)
    return created.id


async def _seed(
    actor: StaffActor | None,
    action: StaffAction,
    request_id: uuid.UUID,
    at: datetime,
    *,
    category_id: uuid.UUID,
    category_id_to: uuid.UUID | None = None,
) -> None:
    """Строка журнала боевой функцией, но с заданным моментом; `actor` None —
    строка без личности (учётка удалена, `actor_user_id` = NULL)."""
    record = StaffActionRecord(
        action=action,
        request_id=request_id,
        category_id=category_id,
        category_id_to=category_id_to,
    )
    async with session_scope() as session:
        if actor is None:
            record_automatic_action(session, record)
        else:
            record_staff_action(session, actor, record)
        (row,) = list(session.new)
        assert isinstance(row, StaffActionLog)
        row.occurred_at = at


def _by_login(rows: list[StaffStatsRow]) -> dict[str | None, StaffStatsRow]:
    return {row.login: row for row in rows}


async def test_return_counts_for_the_one_who_claimed_and_resets_the_wait(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 1: A взял, старший вернул, B взял и выполнил. Возврат — у A, не у
    вернувшего; ожидание B — от возврата, не от создания."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id)
    category = await make_category(tenant_id)
    first, second, senior = (
        await _actor("A", "AAA001"),
        await _actor("B", "BBB001"),
        await _actor("S", "SSS001"),
    )
    with tenant_context(tenant_id):
        request_id = await _request(category.id)
        await _seed(
            first, StaffAction.REQUEST_CLAIMED, request_id, T0 + 10 * MIN, category_id=category.id
        )
        await _seed(
            senior, StaffAction.REQUEST_RETURNED, request_id, T0 + 30 * MIN, category_id=category.id
        )
        await _seed(
            second, StaffAction.REQUEST_CLAIMED, request_id, T0 + 35 * MIN, category_id=category.id
        )
        await _seed(
            second, StaffAction.REQUEST_DONE, request_id, T0 + 65 * MIN, category_id=category.id
        )
        await shift_request(request_id, claimed_at=T0 + 35 * MIN)
        rows = _by_login(await staff_stats(since=SINCE, until=UNTIL, category_ids=None))

    assert set(rows) == {"AAA001", "BBB001"}
    assert (rows["AAA001"].claimed, rows["AAA001"].returned, rows["AAA001"].done) == (1, 1, 0)
    assert rows["AAA001"].claim_wait_median_seconds == 600
    assert rows["AAA001"].work_median_seconds is None
    assert (rows["BBB001"].claimed, rows["BBB001"].returned, rows["BBB001"].done) == (1, 0, 1)
    assert rows["BBB001"].claim_wait_median_seconds == 300
    assert rows["BBB001"].work_median_seconds == 1800


async def test_transfer_starts_the_wait_and_is_not_a_return(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 2: заявку передали в инженерию через час, взяли через 5 мин —
    ждали 5 мин, не просрочено. Передача взятой — не «Возвращено»."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id, reminder_after_minutes=15)
    reception = await make_category(tenant_id, key="reception", name="Ресепшен")
    engineering = await make_category(tenant_id, key="engineering", name="Инженерия")
    clerk, engineer = await _actor("R", "RRR001"), await _actor("E", "EEE001")
    with tenant_context(tenant_id):
        moved = await _request(reception.id)
        await _seed(
            clerk,
            StaffAction.REQUEST_TRANSFERRED,
            moved,
            T0 + 60 * MIN,
            category_id=reception.id,
            category_id_to=engineering.id,
        )
        await _seed(
            engineer, StaffAction.REQUEST_CLAIMED, moved, T0 + 65 * MIN, category_id=engineering.id
        )
        handed = await _request(reception.id)
        await _seed(
            clerk, StaffAction.REQUEST_CLAIMED, handed, T0 + 5 * MIN, category_id=reception.id
        )
        await _seed(
            clerk,
            StaffAction.REQUEST_TRANSFERRED,
            handed,
            T0 + 10 * MIN,
            category_id=reception.id,
            category_id_to=engineering.id,
        )
        rows = _by_login(await staff_stats(since=SINCE, until=UNTIL, category_ids=None))

    assert rows["EEE001"].claim_wait_median_seconds == 300
    assert rows["EEE001"].overdue == 0
    assert (rows["RRR001"].claimed, rows["RRR001"].returned) == (1, 0)


async def test_overdue_uses_the_deadline_of_the_claiming_service(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 3: ждали ровно срок — просрочено; у службы срока нет — нет. Срок —
    службы строки взятия: заявку, переданную после взятия в службу без срока,
    просрочку это не снимает (запись later о ревью PR #712)."""
    tenant_id, _ = two_tenants
    await store_hotel_config(
        tenant_id, reminder_after_minutes=None, reminder_minutes_by_category={"housekeeping": 10}
    )
    housekeeping = await make_category(tenant_id)
    engineering = await make_category(tenant_id, key="engineering", name="Инженерия")
    maid, engineer = await _actor("H", "HHH001"), await _actor("E", "EEE001")
    with tenant_context(tenant_id):
        on_time = await _request(housekeeping.id)
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, on_time, T0 + 9 * MIN, category_id=housekeeping.id
        )
        exactly = await _request(housekeeping.id)
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, exactly, T0 + 10 * MIN, category_id=housekeeping.id
        )
        late_then_moved = await _request(housekeeping.id)
        await _seed(
            maid,
            StaffAction.REQUEST_CLAIMED,
            late_then_moved,
            T0 + 12 * MIN,
            category_id=housekeeping.id,
        )
        await _seed(
            maid,
            StaffAction.REQUEST_TRANSFERRED,
            late_then_moved,
            T0 + 20 * MIN,
            category_id=housekeeping.id,
            category_id_to=engineering.id,
        )
        no_deadline = await _request(engineering.id)
        await _seed(
            engineer,
            StaffAction.REQUEST_CLAIMED,
            no_deadline,
            T0 + 90 * MIN,
            category_id=engineering.id,
        )
        rows = _by_login(await staff_stats(since=SINCE, until=UNTIL, category_ids=None))

    assert (rows["HHH001"].claimed, rows["HHH001"].overdue) == (3, 2)
    assert rows["HHH001"].claim_wait_median_seconds == 600
    assert rows["EEE001"].overdue == 0


async def test_overdue_is_none_when_hotel_reminders_are_off(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 3: напоминания у отеля выключены — `None` (страница: «—»), а не ноль."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id, reminder_after_minutes=None)
    category = await make_category(tenant_id)
    maid = await _actor("H", "HHH001")
    with tenant_context(tenant_id):
        request_id = await _request(category.id)
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, request_id, T0 + 90 * MIN, category_id=category.id
        )
        (row,) = await staff_stats(since=SINCE, until=UNTIL, category_ids=None)

    assert (row.claimed, row.overdue, row.claim_wait_median_seconds) == (1, None, 5400)


async def test_period_and_scope_cut_only_claims_and_done(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 5 и §2.3: старшая уборки видит взятия и «Готово» уборки — и ресепшениста
    с ними; инженера без строк уборки у неё нет. Передача из чужой службы до
    периода всё равно сдвигает отсчёт. «Готово» в периоде без взятия в нём —
    строка есть. Строка без личности не считается (§6 п. 7)."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id)
    housekeeping = await make_category(tenant_id)
    reception = await make_category(tenant_id, key="reception", name="Ресепшен")
    engineering = await make_category(tenant_id, key="engineering", name="Инженерия")
    clerk, engineer, maid = (
        await _actor("R", "RRR001"),
        await _actor("E", "EEE001"),
        await _actor("H", "HHH001"),
    )
    with tenant_context(tenant_id):
        moved = await _request(reception.id, created_at=SINCE - 120 * MIN)
        await _seed(
            clerk,
            StaffAction.REQUEST_TRANSFERRED,
            moved,
            SINCE - 10 * MIN,
            category_id=reception.id,
            category_id_to=housekeeping.id,
        )
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, moved, SINCE + 5 * MIN, category_id=housekeeping.id
        )
        by_clerk = await _request(housekeeping.id)
        await _seed(
            clerk, StaffAction.REQUEST_CLAIMED, by_clerk, T0 + 3 * MIN, category_id=housekeeping.id
        )
        foreign = await _request(engineering.id)
        await _seed(
            engineer, StaffAction.REQUEST_CLAIMED, foreign, T0 + 3 * MIN, category_id=engineering.id
        )
        before = await _request(housekeeping.id, created_at=SINCE - 60 * MIN)
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, before, SINCE - 30 * MIN, category_id=housekeeping.id
        )
        await _seed(maid, StaffAction.REQUEST_DONE, before, T0, category_id=housekeeping.id)
        await shift_request(before, claimed_at=SINCE - 30 * MIN)
        orphan = await _request(housekeeping.id)
        await _seed(
            None, StaffAction.REQUEST_CLAIMED, orphan, T0 + MIN, category_id=housekeeping.id
        )
        rows = _by_login(
            await staff_stats(since=SINCE, until=UNTIL, category_ids=frozenset({housekeeping.id}))
        )
        everyone = _by_login(await staff_stats(since=SINCE, until=UNTIL, category_ids=None))

    assert set(rows) == {"RRR001", "HHH001"}
    assert (rows["RRR001"].claimed, rows["RRR001"].claim_wait_median_seconds) == (1, 180)
    assert (rows["HHH001"].claimed, rows["HHH001"].done) == (1, 1)
    assert rows["HHH001"].claim_wait_median_seconds == 900
    assert rows["HHH001"].work_median_seconds == int((T0 - SINCE + 30 * MIN).total_seconds())
    assert set(everyone) == {"RRR001", "HHH001", "EEE001"}
    assert sum(row.claimed for row in everyone.values()) == 3


async def test_live_status_changes_feed_the_same_numbers(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Боевой путь: переходы статуса с личностью пишут журнал, а итоги читают
    его — взял, вернул, взял другой и выполнил."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id)
    category = await make_category(tenant_id)
    first, second = await make_acting_user("A"), await make_acting_user("B")
    first_actor = first.model_copy(update={"login": "AAA001"})
    second_actor = second.model_copy(update={"login": "BBB001"})
    with tenant_context(tenant_id):
        created = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="guest request",
                room_number="305",
            )
        )
        for status, actor in (
            (RequestStatus.IN_PROGRESS, first_actor),
            (RequestStatus.NEW, first_actor),
            (RequestStatus.IN_PROGRESS, second_actor),
            (RequestStatus.DONE, second_actor),
        ):
            await change_request_status(created.id, status, acting_user=actor)
        now = utc_now()
        rows = _by_login(
            await staff_stats(since=now - timedelta(hours=1), until=now + MIN, category_ids=None)
        )

    assert (rows["AAA001"].claimed, rows["AAA001"].returned) == (1, 1)
    assert (rows["BBB001"].claimed, rows["BBB001"].done, rows["BBB001"].returned) == (1, 1, 0)
    assert rows["BBB001"].work_median_seconds is not None


async def test_other_tenant_rows_never_count(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """§6 п. 8 (P-4): строки журнала тенанта A не попадают в итоги тенанта B."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)
    maid = await _actor("H", "HHH001")
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        await _seed(
            maid, StaffAction.REQUEST_CLAIMED, request_id, T0 + MIN, category_id=category.id
        )
        assert len(await staff_stats(since=SINCE, until=UNTIL, category_ids=None)) == 1
    with tenant_context(tenant_b):
        assert await staff_stats(since=SINCE, until=UNTIL, category_ids=None) == []


def test_median_ignores_a_day_long_outlier() -> None:
    """§6 п. 9: нечётное и чётное число значений; выброс в сутки медиану не сдвигает."""
    minutes = [timedelta(minutes=value) for value in (1, 2, 3)]
    assert _median_seconds(minutes) == 120
    assert _median_seconds(minutes[:2]) == 90
    assert _median_seconds([*minutes, timedelta(days=1)]) == 150
    assert _median_seconds([]) is None


async def test_claim_right_after_claim_is_not_a_return(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.2: между двумя взятиями нет строки возврата (вернули из Telegram) —
    исход первого неизвестен, «Возвращено» ему не пишется."""
    tenant_id, _ = two_tenants
    await store_hotel_config(tenant_id)
    category = await make_category(tenant_id)
    first, second = await _actor("A", "AAA001"), await _actor("B", "BBB001")
    with tenant_context(tenant_id):
        request_id = await _request(category.id)
        await _seed(
            first, StaffAction.REQUEST_CLAIMED, request_id, T0 + MIN, category_id=category.id
        )
        await _seed(
            second, StaffAction.REQUEST_CLAIMED, request_id, T0 + 9 * MIN, category_id=category.id
        )
        await _seed(
            second, StaffAction.REQUEST_RETURNED, request_id, T0 + 20 * MIN, category_id=category.id
        )
        rows = _by_login(await staff_stats(since=SINCE, until=UNTIL, category_ids=None))

    assert (rows["AAA001"].claimed, rows["AAA001"].returned) == (1, 0)
    assert (rows["BBB001"].claimed, rows["BBB001"].returned) == (1, 1)

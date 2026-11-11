"""Числа заявок за период — лист «Отчёт» (spec 0083 §6 п. 1, issue #950).

Сценарии — сдвигом меток времени в БД на фиксированные даты прошлого (канон
`test_day_summary`): неделя 11–17 мая 2026 (пн–вс), пояс отеля Asia/Almaty.
Предикат просрочки — общий с `day_summary`, его крайние случаи стерегут тесты
сводки дня; здесь — то, что добавляет период: окна, медианы, отсечка по концу
периода, а не суток.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from hospitality.modules.requests.api import (
    RequestStatus,
    change_request_status,
    day_summary,
    period_summary,
)
from hospitality.modules.requests.tests.conftest import (
    make_category,
    shift_request,
    store_hotel_config,
)
from hospitality.modules.requests.tests.test_day_summary import (
    hotel_moment,
    hotel_noon,
    make_request,
)
from hospitality.shared.tenancy import tenant_context

MONDAY = date(2026, 5, 11)
SUNDAY = date(2026, 5, 17)


async def test_created_done_cancelled_by_their_own_windows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Создано — по `service_day`, выполнено и отменено — по `closed_at`: заявка
    прошлой недели, выполненная в этой, стоит в «Выполнено» этой и в «Создано» той."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a, reminder_after_minutes=None)
    cleaning = await make_category(tenant_a)
    repair = await make_category(tenant_a, key="maintenance", name="Maintenance")
    early = await make_request(tenant_a, cleaning.id, summary="прошлая неделя")
    done = await make_request(tenant_a, cleaning.id, summary="выполнена")
    cancelled = await make_request(tenant_a, repair.id, summary="отменена")
    with tenant_context(tenant_a):
        for request_id in (early, done):
            await change_request_status(request_id, RequestStatus.IN_PROGRESS)
            await change_request_status(request_id, RequestStatus.DONE)
        await change_request_status(cancelled, RequestStatus.CANCELLED)
        before = MONDAY - timedelta(days=2)
        await shift_request(
            early,
            service_day=before,
            created_at=hotel_noon(before),
            claimed_at=hotel_noon(before) + timedelta(minutes=5),
            closed_at=hotel_noon(MONDAY),
        )
        await shift_request(
            done,
            service_day=MONDAY,
            created_at=hotel_noon(MONDAY),
            claimed_at=hotel_noon(MONDAY) + timedelta(minutes=10),
            closed_at=hotel_noon(MONDAY) + timedelta(minutes=40),
        )
        await shift_request(
            cancelled,
            service_day=SUNDAY,
            created_at=hotel_noon(SUNDAY),
            closed_at=hotel_noon(SUNDAY) + timedelta(minutes=1),
        )
        week = await period_summary(MONDAY, SUNDAY)
        previous = await period_summary(MONDAY - timedelta(days=7), MONDAY - timedelta(days=1))

    assert (week.total.created, week.total.done, week.total.cancelled) == (2, 2, 1)
    assert week.total.overdue is None  # напоминания выключены — прочерк, а не ноль
    assert (previous.total.created, previous.total.done) == (1, 0)
    by_service = {row.category_id: row for row in week.by_service}
    assert (by_service[cleaning.id].created, by_service[cleaning.id].done) == (1, 2)
    assert (by_service[repair.id].created, by_service[repair.id].cancelled) == (1, 1)
    assert week.period_start == hotel_moment(MONDAY, 0)
    assert week.period_end == hotel_moment(SUNDAY + timedelta(days=1), 0)


async def test_medians_count_from_creation(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """«Взяли за» — медиана от создания до взятия по взятым в окне, «Выполнили
    за» — от создания до «Готово» по выполненным в окне: сколько ждал гость."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    waits = ((2, 30), (4, 50), (60, 90))  # минуты: (до взятия, до «Готово»)
    ids = [await make_request(tenant_a, category.id, summary=f"№{i}") for i in range(3)]
    with tenant_context(tenant_a):
        for request_id, (claim, finish) in zip(ids, waits, strict=True):
            await change_request_status(request_id, RequestStatus.IN_PROGRESS)
            await change_request_status(request_id, RequestStatus.DONE)
            created_at = hotel_noon(MONDAY + timedelta(days=1))
            await shift_request(
                request_id,
                service_day=MONDAY + timedelta(days=1),
                created_at=created_at,
                claimed_at=created_at + timedelta(minutes=claim),
                closed_at=created_at + timedelta(minutes=finish),
            )
        week = await period_summary(MONDAY, SUNDAY)

    assert week.total.claim_median_seconds == 4 * 60
    assert week.total.done_median_seconds == 50 * 60
    (row,) = week.by_service
    assert row.done_median_seconds == 50 * 60


async def test_overdue_cutoff_is_the_end_of_the_period_not_of_the_day(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Заявку создали в понедельник в 23:50 при сроке 30 минут и не взяли: сводка
    понедельника просрочки не видит — сутки кончились через десять минут, — а
    неделя видит: заявка ждала до конца периода. Созданная в воскресенье в 23:50
    просрочку не успевает получить и в неделе."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a, reminder_after_minutes=30)
    category = await make_category(tenant_a)
    monday_late = await make_request(tenant_a, category.id, summary="понедельник ночью")
    sunday_late = await make_request(tenant_a, category.id, summary="воскресенье ночью")
    with tenant_context(tenant_a):
        await shift_request(
            monday_late, service_day=MONDAY, created_at=hotel_moment(MONDAY, 23, 50)
        )
        await shift_request(
            sunday_late, service_day=SUNDAY, created_at=hotel_moment(SUNDAY, 23, 50)
        )
        week = await period_summary(MONDAY, SUNDAY)
        monday = await day_summary(MONDAY)

    assert monday.overdue == 0
    assert week.total.overdue == 1
    assert week.total.created == 2


async def test_other_tenant_requests_are_not_counted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """RLS: заявки второго отеля в числа первого не попадают."""
    tenant_a, tenant_b = two_tenants
    category_b = await make_category(tenant_b)
    foreign = await make_request(tenant_b, category_b.id, summary="чужая")
    with tenant_context(tenant_b):
        await shift_request(foreign, service_day=MONDAY, created_at=hotel_noon(MONDAY))
    with tenant_context(tenant_a):
        week = await period_summary(MONDAY, SUNDAY)

    assert week.total.created == 0
    assert week.by_service == []

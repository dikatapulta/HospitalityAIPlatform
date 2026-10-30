"""Счёт квоты бесплатного на сегодня (spec 0056 Р-2, Р-3, §7).

`check_quota` отвечает на «сколько уже заказано и сколько ещё бесплатно»;
что сказать гостю, решает инструмент (PR 2 серии). Метка квоты — аргументы
`create_request`, а не поля HTTP-схемы, как `stay_id`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.requests import service
from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    check_quota,
    create_request,
    get_request,
)
from hospitality.modules.requests.tests.conftest import make_category, store_hotel_config
from hospitality.platform.config import LocalizedText
from hospitality.platform.service_quotas import ServiceQuota
from hospitality.shared.tenancy import tenant_context

_WATER = ServiceQuota(
    key="water",
    names=LocalizedText(en="Bottled water", ru="Бутилированная вода"),
    category_key="housekeeping",
    per_guest_per_day=1,
)
_ALMATY = ZoneInfo("Asia/Almaty")


async def _order(
    category_id: uuid.UUID,
    *,
    stay_id: uuid.UUID | None,
    quota_key: str | None = "water",
    quota_units: int | None = 1,
) -> ServiceRequestRead:
    return await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=ServiceRequestOrigin.GUEST_CHAT,
            summary="вода",
            room_number="704",
        ),
        stay_id=stay_id,
        quota_key=quota_key,
        quota_units=quota_units,
    )


async def test_limit_is_norm_times_guests_and_open_requests_count(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Р-2, Р-3: трое гостей — три бутылки в день; открытая заявка уже в счёте,
    иначе пять просьб подряд до первой доставки прошли бы бесплатно."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    stay = uuid.uuid4()

    with tenant_context(tenant_a):
        stored = await _order(category.id, stay_id=stay, quota_units=2)
        check = await check_quota(_WATER, stay_id=stay, guests_count=3, units=2)
        reread = await get_request(stored.id)

    assert (reread.quota_key, reread.quota_units) == ("water", 2)
    assert (check.limit, check.used, check.free_units) == (3, 2, 1)


async def test_cancelled_paid_and_foreign_requests_are_not_counted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отмена норму возвращает; платная заявка ключа не несёт; заявка другого
    Stay той же комнаты и другой квоты — не наш счёт."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    stay, previous_guest = uuid.uuid4(), uuid.uuid4()

    with tenant_context(tenant_a):
        cancelled = await _order(category.id, stay_id=stay)
        await change_request_status(cancelled.id, RequestStatus.CANCELLED)
        await _order(category.id, stay_id=stay, quota_key=None, quota_units=None)
        await _order(category.id, stay_id=previous_guest)
        await _order(category.id, stay_id=stay, quota_key="towels")
        check = await check_quota(_WATER, stay_id=stay, guests_count=1, units=1)

    assert (check.used, check.free_units) == (0, 1)


async def test_exhausted_quota_leaves_nothing_free(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сверх нормы остаток ноль, а не отрицательное число."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    stay = uuid.uuid4()

    with tenant_context(tenant_a):
        await _order(category.id, stay_id=stay, quota_units=3)
        check = await check_quota(_WATER, stay_id=stay, guests_count=2, units=1)

    assert (check.limit, check.used, check.free_units) == (2, 3, 0)


async def test_day_is_the_hotel_day(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Р-2: полночь — по поясу отеля, та же, что у дневного номера. Заявка в
    23:50 по Алматы — вчерашняя для проверки в 00:10, хотя по UTC это один день."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    stay = uuid.uuid4()
    late_evening = datetime(2026, 10, 30, 23, 50, tzinfo=_ALMATY).astimezone(UTC)
    after_midnight = datetime(2026, 10, 31, 0, 10, tzinfo=_ALMATY).astimezone(UTC)
    assert late_evening.date() == after_midnight.date()

    with tenant_context(tenant_a):
        monkeypatch.setattr(service, "utc_now", lambda: late_evening)
        await _order(category.id, stay_id=stay)
        same_evening = await check_quota(_WATER, stay_id=stay, guests_count=1, units=1)
        monkeypatch.setattr(service, "utc_now", lambda: after_midnight)
        next_day = await check_quota(_WATER, stay_id=stay, guests_count=1, units=1)
        await _order(category.id, stay_id=stay)
        next_day_after_order = await check_quota(_WATER, stay_id=stay, guests_count=1, units=1)

    assert same_evening.used == 1
    assert next_day.used == 0
    assert next_day_after_order.used == 1


@pytest.mark.parametrize(
    ("quota_key", "quota_units"),
    [("water", None), (None, 1), ("water", 0), ("water", 21)],
)
async def test_quota_mark_is_a_pair_of_one_to_twenty_units(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    quota_key: str | None,
    quota_units: int | None,
) -> None:
    """Ключ без штук или штуки вне 1–20 — ошибка вызывающего кода до записи."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a), pytest.raises(ValueError, match="quota"):
        await _order(
            category.id, stay_id=uuid.uuid4(), quota_key=quota_key, quota_units=quota_units
        )


async def test_request_without_quota_has_no_mark(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Все прежние пути заявок (кабинет, Telegram, HTTP API) метки не ставят."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        created = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.STAFF_MANUAL,
                summary="перегорела лампа",
            )
        )

    assert (created.quota_key, created.quota_units) == (None, None)

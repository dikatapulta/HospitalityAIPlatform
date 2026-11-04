"""Запросы витрины для кабинета и их срок 365 дней (spec 0069 §3, §7 п. 6; PR (в), #805)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.request_models import ShowroomRequest
from hospitality.modules.showroom.tests.request_fixtures import (
    Hotel,
    booking,
    group,
    make_hotel,
    send,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("scene")


@pytest.fixture
async def hotel(canonical_database: None) -> Hotel:
    return await make_hotel("showroom-hotel")


async def _age(hotel: Hotel, days: int, *, request_created: bool | None = None) -> None:
    """Состарить все запросы отеля на `days` дней."""
    values: dict[str, object] = {"created_at": utc_now() - timedelta(days=days)}
    if request_created is not None:
        values["request_created"] = request_created
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(update(ShowroomRequest).values(**values))


async def test_card_reads_contacts_from_module(hotel: Hotel) -> None:
    placed = await send(hotel, booking(wish_type_key="deluxe", wishes="Late arrival"))

    with tenant_context(hotel.tenant_id):
        found = await showroom_api.requests_for_requests([placed.request.request_id, uuid.uuid4()])

    [read] = found.values()
    assert read.kind is showroom_api.ShowroomRequestKind.BOOKING
    assert (read.contact_name, read.phone, read.email) == ("Ержан", "+7 777 000 11 22", None)
    assert read.wish_view_names is not None and read.wish_view_names.ru == "Горы"
    assert read.wish_type_names is not None and read.wish_type_names.ru == "Делюкс"
    assert read.wishes == "Late arrival"
    assert read.anonymized is False


async def test_other_hotel_sees_nothing(hotel: Hotel) -> None:
    placed = await send(hotel, group())
    other = await make_hotel("other-hotel")

    with tenant_context(other.tenant_id):
        assert await showroom_api.requests_for_requests([placed.request.request_id]) == {}


async def test_contacts_anonymized_after_365_days_only(hotel: Hotel) -> None:
    placed = await send(hotel, group())
    await _age(hotel, 364)

    with tenant_context(hotel.tenant_id):
        young = await showroom_api.anonymize_expired(utc_now())
    assert young == showroom_api.ShowroomRetention(0, 0)

    await _age(hotel, 366)
    with tenant_context(hotel.tenant_id):
        old = await showroom_api.anonymize_expired(utc_now())
        again = await showroom_api.anonymize_expired(utc_now())
        read = (await showroom_api.requests_for_requests([placed.request.request_id]))[
            placed.request.request_id
        ]

    assert old == showroom_api.ShowroomRetention(1, 0)
    assert again == showroom_api.ShowroomRetention(0, 0)
    assert read.anonymized is True
    assert (read.company, read.contact_name, read.phone, read.email, read.wishes) == (
        None,
        None,
        None,
        None,
        None,
    )
    # Для отчётов остаётся всё, что не про человека.
    assert (read.guests_count, read.room_numbers) == (32, ["1901", "1903"])


async def test_abandoned_row_without_request_deleted_after_a_day(hotel: Hotel) -> None:
    await send(hotel, group())
    await _age(hotel, 2, request_created=False)

    with tenant_context(hotel.tenant_id):
        swept = await showroom_api.anonymize_expired(utc_now())
        async with session_scope() as session:
            left = (await session.scalars(select(ShowroomRequest))).all()

    assert swept.abandoned_deleted == 1
    assert left == []

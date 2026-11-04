"""Запрос из 3D-витрины → заявка службе (spec 0069 §2.4, §7 п. 6; PR (в), #805)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom import visitor_requests
from hospitality.modules.showroom.request_models import ShowroomRequest
from hospitality.modules.showroom.request_scene import SceneFacts
from hospitality.modules.showroom.tests.request_fixtures import (
    Hotel,
    booking,
    configure,
    group,
    hotel_today,
    make_hotel,
    send,
    showroom_config,
)
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("scene")


@pytest.fixture
async def hotel(canonical_database: None) -> Hotel:
    return await make_hotel("showroom-hotel")


async def _request(hotel: Hotel, placed: showroom_api.ShowroomRequestPlaced) -> Any:
    with tenant_context(hotel.tenant_id):
        return await requests_api.get_request(placed.request.request_id)


async def _expect(code: str, hotel: Hotel, data: showroom_api.ShowroomRequestCreate) -> None:
    with pytest.raises(AppError) as error:
        await send(hotel, data)
    assert error.value.code == code


async def _rows(hotel: Hotel) -> list[ShowroomRequest]:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return list((await session.scalars(select(ShowroomRequest))).all())


async def test_group_goes_to_sales_without_contacts_in_request_text(hotel: Hotel) -> None:
    placed = await send(hotel, group())

    request = await _request(hotel, placed)
    assert placed.duplicate is False
    assert request.category_id == hotel.sales_id
    assert request.origin is requests_api.ServiceRequestOrigin.SHOWROOM_FORM
    assert request.room_number is None
    assert request.summary.startswith("Групповой запрос · Silk Road Travel · 32 гост. · ")
    assert request.details == "Номера: 1901, 1903"
    text = f"{request.summary}\n{request.details}"
    for contact in ("Anna Lee", "701 123", "anna@example.com", "Breakfast"):
        assert contact not in text
    [row] = await _rows(hotel)
    assert (row.contact_name, row.email) == ("Anna Lee", "anna@example.com")
    assert row.wishes == "Breakfast at 7"
    assert row.consent_version == "2026-11-04-v1"
    assert row.consent_at is not None
    assert row.request_created is True


async def test_booking_goes_to_reservations_with_wish(hotel: Hotel) -> None:
    placed = await send(hotel, booking(wish_type_key="deluxe"))

    request = await _request(hotel, placed)
    assert request.category_id == hotel.reservations_id
    assert request.summary.endswith("· 2 гост. · вид «Горы», тип «Делюкс»")
    assert request.details == "Желание: вид «Горы», тип «Делюкс»"
    [row] = await _rows(hotel)
    assert row.wish_view_names == {"en": "Mountains", "ru": "Горы", "kk": "Таулар"}


async def test_booking_without_reservations_service_goes_to_sales(hotel: Hotel) -> None:
    await configure(hotel.tenant_id, showroom_config(booking_category_key=None))

    request = await _request(hotel, await send(hotel, booking()))

    assert request.category_id == hotel.sales_id


async def test_repeat_of_client_key_is_the_same_request(hotel: Hotel) -> None:
    first = await send(hotel, group())
    again = await send(hotel, group(company="Other"))

    assert again.duplicate is True
    assert again.request.request_id == first.request.request_id
    assert len(await _rows(hotel)) == 1
    with tenant_context(hotel.tenant_id):
        assert len(await requests_api.list_open_requests(limit=10)) == 1


async def test_repeat_finishes_request_that_failed_halfway(hotel: Hotel) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requests_api, "create_request", broken)
        with pytest.raises(RuntimeError):
            await send(hotel, group())

    placed = await send(hotel, group())

    assert placed.duplicate is True
    assert (await _request(hotel, placed)).category_id == hotel.sales_id


@pytest.mark.parametrize(
    "changes",
    [
        {"check_in": -1, "check_out": 2},
        {"check_in": 5, "check_out": 5},
        {"check_in": 399, "check_out": 401},
        {"room_numbers": ["1901", "7777"]},
        {"wish_view_key": "sea"},
        {"wish_type_key": "suite"},
    ],
)
async def test_out_of_rules_is_004(hotel: Hotel, changes: dict[str, Any]) -> None:
    today = hotel_today()
    for field in ("check_in", "check_out"):
        if field in changes:
            changes[field] = today + timedelta(days=changes[field])

    await _expect(showroom_api.ERR_SHOWROOM_REQUEST_INVALID, hotel, group(**changes))
    assert await _rows(hotel) == []


async def test_booking_guests_over_hotel_limit_is_004(hotel: Hotel) -> None:
    await configure(hotel.tenant_id, showroom_config(booking_max_guests=4))

    await _expect(showroom_api.ERR_SHOWROOM_REQUEST_INVALID, hotel, booking(guests_count=5))
    await send(hotel, booking(guests_count=4))


@pytest.mark.parametrize(
    "changes",
    [
        {"phone": None, "email": None},
        {"company": "  "},
        {"email": "not-an-email"},
        {"phone": "call me"},
        {"guests_count": 501},
        {"room_numbers": ["1901", "1901"]},
        {"wishes": "x" * 501},
    ],
)
def test_form_rules_in_schema(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        group(**changes)


def test_booking_names_at_most_three_rooms() -> None:
    with pytest.raises(ValidationError):
        booking(room_numbers=["1901", "1903", "1905", "2001"])


async def test_disabled_showroom_no_scene_or_no_service_is_001(hotel: Hotel) -> None:
    await configure(hotel.tenant_id, showroom_config(enabled=False))
    await _expect(showroom_api.ERR_SHOWROOM_DISABLED, hotel, group())

    await configure(hotel.tenant_id, None)
    await _expect(showroom_api.ERR_SHOWROOM_DISABLED, hotel, group())

    await configure(hotel.tenant_id, showroom_config(sales_category_key="nobody"))
    await _expect(showroom_api.ERR_SHOWROOM_DISABLED, hotel, group())

    async def no_scene() -> SceneFacts | None:
        return None

    await configure(hotel.tenant_id, showroom_config())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(visitor_requests, "load_scene_facts", no_scene)
        await _expect(showroom_api.ERR_SHOWROOM_DISABLED, hotel, group())
    assert await _rows(hotel) == []


async def test_card_number_in_wishes_is_masked(hotel: Hotel) -> None:
    await send(hotel, group(wishes="Pay by 4111 1111 1111 1111 please"))

    [row] = await _rows(hotel)
    assert row.wishes is not None
    assert "4111 1111" not in row.wishes
    assert "1111]" in row.wishes


async def test_request_counts_in_day_summary_as_showroom(hotel: Hotel) -> None:
    await send(hotel, group())

    with tenant_context(hotel.tenant_id):
        summary = await requests_api.day_summary(hotel_today())
    origin = summary.created_by_origin
    assert origin[requests_api.ServiceRequestOrigin.SHOWROOM_FORM] == 1
    assert origin[requests_api.ServiceRequestOrigin.GUEST_FORM] == 0
    assert sum(origin.values()) == summary.created_total

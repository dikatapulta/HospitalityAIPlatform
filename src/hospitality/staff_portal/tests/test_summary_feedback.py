"""Плитка «Оценки гостей» на «Сводке дня» (spec 0076 §6, §10 — PR 3 серии, #899).

Счёт оценок проверен у владельца (`modules/feedback/tests/test_states.py`); здесь —
что видят глазами менеджер и старший: итог и строки по службам, срез старшего,
плитки нет при выключенных оценках, «Пока нет» — когда оценок за день нет.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient

from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import summary
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

SUMMARY_PAGE = f"/staff/{HOTEL_SLUG}/summary"
TILE = "Оценки гостей"
UP = feedback_api.FeedbackRating.UP
DOWN = feedback_api.FeedbackRating.DOWN


def _feedback_tile(html: str) -> str:
    """Плитка оценок целиком — надписи проверяются внутри неё, а не по всей странице."""
    match = re.search(r'<div class="card tile tile-wide">(.*?)</div>', html, re.S)
    assert match is not None
    return match.group(1)


async def _configure(portal_hotel: PortalHotel, *, enabled: bool = True) -> None:
    await store_hotel_config(
        portal_hotel.tenant_id, escalation_category_key="reception", guest_feedback_enabled=enabled
    )


async def _rated_day(portal_hotel: PortalHotel) -> None:
    """Хаускипинг: 👍 👍 👎; прачечная: 👍. Палец вниз заводит карточку ресепшену."""
    await _configure(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        housekeeping = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Хаускипинг")
        )
        laundry = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="laundry", name="Прачечная")
        )
        stay = await check_in(
            StayCheckIn(room_number="512", check_out_at=utc_now() + timedelta(days=3))
        )
        for category_id, rating in (
            (housekeeping.id, UP),
            (housekeeping.id, UP),
            (housekeeping.id, DOWN),
            (laundry.id, UP),
        ):
            request = await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=category_id,
                    origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                    summary="заявка гостя",
                    room_number="512",
                ),
                stay_id=stay.stay.id,
            )
            await requests_api.change_request_status(
                request.id, requests_api.RequestStatus.IN_PROGRESS
            )
            await requests_api.change_request_status(request.id, requests_api.RequestStatus.DONE)
            await feedback_api.rate_request(request.id, stay_id=stay.stay.id, rating=rating)


async def _login(client: AsyncClient, portal_hotel: PortalHotel, *service_keys: str) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.SENIOR,
        service_keys=service_keys,
    )
    await submit_login(client, staff_login)


async def test_manager_sees_total_and_a_line_per_service(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§8: итог «хорошо 3 · плохо 1» и строки служб — больше оценок выше."""
    await _rated_day(portal_hotel)
    await submit_login(client, portal_hotel.login)

    tile = _feedback_tile((await client.get(SUMMARY_PAGE)).text)

    assert TILE in tile
    assert '<span class="tile-value">хорошо 3 · плохо 1</span>' in tile
    assert tile.index("Хаускипинг — хорошо 2 · плохо 1") < tile.index(
        "Прачечная — хорошо 1 · плохо 0"
    )


async def test_senior_sees_ratings_of_own_services_only(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """spec 0045 §3: старшему хаускипинга — оценки хаускипинга, прачечной нет ни в
    итоге, ни строкой."""
    await _rated_day(portal_hotel)
    await _login(client, portal_hotel, "housekeeping")

    page = (await client.get(SUMMARY_PAGE)).text
    tile = _feedback_tile(page)

    assert '<span class="tile-value">хорошо 2 · плохо 1</span>' in tile
    assert "Хаускипинг — хорошо 2 · плохо 1" in tile
    assert "Прачечная" not in page


async def test_no_tile_when_ratings_are_off(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """§4.4: отель выключил оценки — плитки нет, даже если оценки в базе остались."""
    await _rated_day(portal_hotel)
    await _configure(portal_hotel, enabled=False)
    await submit_login(client, portal_hotel.login)

    page = (await client.get(SUMMARY_PAGE)).text

    assert "Создано" in page  # день не пуст — плиток просто нет одной
    assert TILE not in page


async def test_day_without_ratings_says_so(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """§8: оценки включены, а за день их нет — «Пока нет», без строк служб."""
    await _configure(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Хаускипинг")
        )
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="ещё не выполнена",
            )
        )
    await submit_login(client, portal_hotel.login)

    tile = _feedback_tile((await client.get(SUMMARY_PAGE)).text)

    assert '<span class="tile-value">Пока нет</span>' in tile
    assert "хорошо" not in tile


def test_tile_orders_services_by_ratings_then_name() -> None:
    """Строки — по числу оценок, при равенстве — по имени; служба, которой уже нет
    в списке категорий, — прочерком, а не пропуском: итог обязан сходиться."""
    bar, spa, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    counts = [
        feedback_api.FeedbackCount(category_id=spa, up=1, down=0),
        feedback_api.FeedbackCount(category_id=gone, up=0, down=2),
        feedback_api.FeedbackCount(category_id=bar, up=1, down=0),
    ]

    tile = summary._feedback_tile(counts, {bar: "Бар", spa: "СПА"})

    assert tile["value"] == "хорошо 2 · плохо 2"
    assert tile["lines"] == [
        "— — хорошо 0 · плохо 2",
        "Бар — хорошо 1 · плохо 0",
        "СПА — хорошо 1 · плохо 0",
    ]
    assert summary._feedback_tile([], {}) == {"value": "Пока нет", "lines": []}


def test_a_rating_keeps_the_day_from_being_empty() -> None:
    """Гость оценил вчерашнюю заявку в день без новых: фраза «заявок не было»
    спрятала бы оценку, поэтому такой день не пуст."""
    start = datetime(2026, 11, 10, tzinfo=UTC)
    quiet_day = requests_api.RequestsDaySummary(
        service_day=date(2026, 11, 10),
        day_start=start,
        day_end=start + timedelta(days=1),
        created_total=0,
        created_by_origin=dict.fromkeys(requests_api.ServiceRequestOrigin, 0),
        closed_total=0,
        closed_done=0,
        closed_cancelled=0,
        by_service=[],
        claim_median_seconds=None,
        overdue=None,
        open_now=0,
    )

    assert summary._is_empty(quiet_day, None, rated=False)
    assert not summary._is_empty(quiet_day, None, rated=True)

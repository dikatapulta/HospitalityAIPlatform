"""Состояние виджета, счёт для сводки, срок комментария, RLS и выключатель
(spec 0076 §4.2, §4.4, §4.5, §10 PR 1)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.feedback.tests.conftest import (
    Hotel,
    configure,
    expect_code,
    guest_request,
    make_hotel,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context

UP = feedback_api.FeedbackRating.UP
DOWN = feedback_api.FeedbackRating.DOWN


async def test_states_cover_only_done_requests_of_this_stay(hotel: Hotel) -> None:
    rated = await guest_request(hotel)
    fresh = await guest_request(hotel)
    open_request = await guest_request(hotel, status=requests_api.RequestStatus.IN_PROGRESS)
    with tenant_context(hotel.tenant_id):
        await feedback_api.rate_request(rated.id, stay_id=hotel.stay_id, rating=DOWN)
        await feedback_api.add_comment(rated.id, stay_id=hotel.stay_id, comment="долго ждал")

        states = await feedback_api.feedback_states(
            [rated.id, fresh.id, open_request.id], stay_id=hotel.stay_id
        )
        foreign = await feedback_api.feedback_states([rated.id], stay_id=uuid.uuid4())

    assert states == {
        rated.id: feedback_api.FeedbackState(rating=DOWN, commented=True),
        fresh.id: feedback_api.FeedbackState(rating=None, commented=False),
    }
    assert foreign == {}


async def test_switched_off_feedback_has_no_states(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    await configure(hotel.tenant_id, enabled=False)

    with tenant_context(hotel.tenant_id):
        assert await feedback_api.feedback_states([request.id], stay_id=hotel.stay_id) == {}


async def test_counts_by_service_in_the_window(hotel: Hotel) -> None:
    for rating in (UP, UP, DOWN):
        request = await guest_request(hotel)
        with tenant_context(hotel.tenant_id):
            await feedback_api.rate_request(request.id, stay_id=hotel.stay_id, rating=rating)
    other = await make_hotel("other-hotel")
    foreign = await guest_request(other)
    with tenant_context(other.tenant_id):
        await feedback_api.rate_request(foreign.id, stay_id=other.stay_id, rating=DOWN)

    start, end = utc_now() - timedelta(hours=1), utc_now() + timedelta(hours=1)
    with tenant_context(hotel.tenant_id):
        counts = await feedback_api.count_feedback(start=start, end=end)
        scoped = await feedback_api.count_feedback(
            start=start, end=end, category_ids=[hotel.reception_id]
        )
        past = await feedback_api.count_feedback(start=start - timedelta(days=1), end=start)

    assert counts == [feedback_api.FeedbackCount(category_id=hotel.housekeeping_id, up=2, down=1)]
    assert scoped == []
    assert past == []


async def test_retention_clears_the_comment_and_keeps_the_rating(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    with tenant_context(hotel.tenant_id):
        await feedback_api.rate_request(request.id, stay_id=hotel.stay_id, rating=DOWN)
        await feedback_api.add_comment(request.id, stay_id=hotel.stay_id, comment="пыль")

        assert await feedback_api.clear_expired_comments(utc_now() - timedelta(days=1)) == 0
        assert await feedback_api.clear_expired_comments(utc_now() + timedelta(seconds=1)) == 1
        assert await feedback_api.clear_expired_comments(utc_now() + timedelta(seconds=1)) == 0

        states = await feedback_api.feedback_states([request.id], stay_id=hotel.stay_id)
        # Обнулённый комментарий второго не открывает: он уже был.
        await expect_code(
            feedback_api.ERR_FEEDBACK_COMMENT_NOT_APPLICABLE,
            feedback_api.add_comment(request.id, stay_id=hotel.stay_id, comment="снова"),
        )
    assert states[request.id] == feedback_api.FeedbackState(rating=DOWN, commented=True)


async def test_retention_of_one_tenant_does_not_touch_another(hotel: Hotel) -> None:
    other = await make_hotel("other-hotel")
    request = await guest_request(other)
    with tenant_context(other.tenant_id):
        await feedback_api.rate_request(request.id, stay_id=other.stay_id, rating=DOWN)
        await feedback_api.add_comment(request.id, stay_id=other.stay_id, comment="пыль")

    with tenant_context(hotel.tenant_id):
        assert await feedback_api.clear_expired_comments(utc_now() + timedelta(seconds=1)) == 0


def test_feedback_cannot_be_switched_on_without_an_escalation_service() -> None:
    with pytest.raises(ValidationError, match="escalation_category_key"):
        TenantConfig(
            profile=HotelProfile(city="Almaty", country_code="KZ"),
            timezone="Asia/Almaty",
            default_language="ru",
            guest_feedback_enabled=True,
        )

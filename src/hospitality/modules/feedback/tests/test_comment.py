"""Комментарий к пальцу вниз (spec 0076 §4.2, §4.3, §10 PR 1).

В невзятую карточку — дописка; взятую — вторая карточка; карточки нет (сбой при
оценке) — комментарий заводит её сам (запись «потом» ревью PR #914). Номер карты
маскируется до записи (NG-3).
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.feedback.tests.conftest import (
    Hotel,
    configure,
    expect_code,
    feedback_cards,
    guest_request,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

UP = feedback_api.FeedbackRating.UP
DOWN = feedback_api.FeedbackRating.DOWN


async def _rated(hotel: Hotel, rating: feedback_api.FeedbackRating) -> uuid.UUID:
    request = await guest_request(hotel)
    with tenant_context(hotel.tenant_id):
        await feedback_api.rate_request(request.id, stay_id=hotel.stay_id, rating=rating)
    return request.id


async def _comment(hotel: Hotel, request_id: uuid.UUID, text: str) -> feedback_api.FeedbackRead:
    with tenant_context(hotel.tenant_id):
        return await feedback_api.add_comment(request_id, stay_id=hotel.stay_id, comment=text)


async def test_comment_goes_into_the_unclaimed_card(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)

    result = await _comment(hotel, request_id, "  пыль под кроватью ")

    assert (result.commented, result.passed_to_staff) == (True, True)
    [card] = await feedback_cards(hotel)
    assert card.details == "Закрыл: Айгерим\nГость написал: «пыль под кроватью»"


async def test_comment_after_the_card_was_taken_opens_a_second_card(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)
    [card] = await feedback_cards(hotel)
    with tenant_context(hotel.tenant_id):
        await requests_api.change_request_status(
            card.id, requests_api.RequestStatus.IN_PROGRESS, acting_user=hotel.staff
        )
        rated = await requests_api.get_request(request_id)

    result = await _comment(hotel, request_id, "пыль под кроватью")

    assert result.passed_to_staff is True
    first, second = await feedback_cards(hotel)
    assert first.details == "Закрыл: Айгерим"
    assert second.summary == (
        f"Гость дописал к оценке заявки #{rated.daily_number}: «пыль под кроватью»"
    )
    assert second.details is None
    assert (second.category_id, second.room_number) == (hotel.reception_id, "512")


async def test_comment_creates_the_card_when_the_rating_could_not(hotel: Hotel) -> None:
    """Сбой карточки при оценке — комментарий её вторая попытка доставки."""
    await configure(hotel.tenant_id, escalation_key="front-desk")  # службы нет
    request_id = await _rated(hotel, DOWN)
    assert await feedback_cards(hotel) == []
    await configure(hotel.tenant_id)  # службу вернули

    result = await _comment(hotel, request_id, "пыль под кроватью")

    assert result.passed_to_staff is True
    [card] = await feedback_cards(hotel)
    assert card.summary.startswith("Гость недоволен заявкой #")
    assert card.details == "Закрыл: Айгерим\nГость написал: «пыль под кроватью»"


async def test_comment_card_failure_is_not_passed_to_staff(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)
    [card] = await feedback_cards(hotel)
    with tenant_context(hotel.tenant_id):
        await requests_api.change_request_status(
            card.id, requests_api.RequestStatus.IN_PROGRESS, acting_user=hotel.staff
        )
    await configure(hotel.tenant_id, escalation_key="front-desk")

    result = await _comment(hotel, request_id, "пыль под кроватью")

    assert (result.commented, result.passed_to_staff) == (True, False)
    assert len(await feedback_cards(hotel)) == 1


async def test_card_number_is_masked_before_it_is_stored(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)

    await _comment(hotel, request_id, "списали дважды с 4111 1111 1111 1111")

    [card] = await feedback_cards(hotel)
    assert "4111 1111" not in (card.details or "")
    assert "[card ****1111]" in (card.details or "")


async def test_comment_to_thumbs_up_is_409(hotel: Hotel) -> None:
    request_id = await _rated(hotel, UP)

    await expect_code(
        feedback_api.ERR_FEEDBACK_COMMENT_NOT_APPLICABLE, _comment(hotel, request_id, "всё ок")
    )


async def test_second_comment_is_409(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)
    await _comment(hotel, request_id, "пыль под кроватью")

    await expect_code(
        feedback_api.ERR_FEEDBACK_COMMENT_NOT_APPLICABLE, _comment(hotel, request_id, "и ещё")
    )


async def test_comment_without_a_rating_is_409(hotel: Hotel) -> None:
    request = await guest_request(hotel)

    await expect_code(
        feedback_api.ERR_FEEDBACK_COMMENT_NOT_APPLICABLE, _comment(hotel, request.id, "плохо")
    )


async def test_comment_of_another_stay_is_404(hotel: Hotel) -> None:
    request_id = await _rated(hotel, DOWN)

    with tenant_context(hotel.tenant_id):
        await expect_code(
            feedback_api.ERR_FEEDBACK_NOT_RATEABLE,
            feedback_api.add_comment(request_id, stay_id=uuid.uuid4(), comment="плохо"),
        )


@pytest.mark.parametrize("text", ["   ", "я" * (feedback_api.COMMENT_MAX_CHARS + 1)])
async def test_empty_or_too_long_comment_is_rejected(hotel: Hotel, text: str) -> None:
    request_id = await _rated(hotel, DOWN)

    with pytest.raises(ValueError):
        await _comment(hotel, request_id, text)

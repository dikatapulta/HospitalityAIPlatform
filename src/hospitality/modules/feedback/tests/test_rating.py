"""Оценка выполненной заявки (spec 0076 §4.2, §4.3, §10 PR 1).

Коды §4.5, двойное касание (одна строка и одна карточка — с принудительным
чередованием, запись «потом» ревью PR #914), карточка пальца вниз в службе
эскалаций, сбой карточки — оценка без карточки и `passed_to_staff=False`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.feedback.tests.conftest import (
    Hotel,
    configure,
    expect_code,
    feedback_cards,
    guest_request,
    make_hotel,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context

UP = feedback_api.FeedbackRating.UP
DOWN = feedback_api.FeedbackRating.DOWN


async def _rate(
    hotel: Hotel, request_id: uuid.UUID, rating: feedback_api.FeedbackRating, **kwargs: Any
) -> feedback_api.FeedbackRead:
    with tenant_context(hotel.tenant_id):
        return await feedback_api.rate_request(
            request_id, stay_id=kwargs.get("stay_id", hotel.stay_id), rating=rating
        )


async def test_thumbs_up_is_saved_without_a_card(hotel: Hotel) -> None:
    request = await guest_request(hotel)

    result = await _rate(hotel, request.id, UP)

    assert (result.rating, result.commented, result.passed_to_staff) == (UP, False, False)
    assert await feedback_cards(hotel) == []


async def test_thumbs_down_opens_a_card_in_the_escalation_service(hotel: Hotel) -> None:
    request = await guest_request(hotel)

    result = await _rate(hotel, request.id, DOWN)

    assert result.passed_to_staff is True
    [card] = await feedback_cards(hotel)
    assert card.category_id == hotel.reception_id
    assert (
        card.summary
        == f"Гость недоволен заявкой #{request.daily_number} «уборка номера» — Хаускипинг"
    )
    assert card.details == "Закрыл: Айгерим"
    assert (card.room_number, card.stay_id, card.is_urgent) == ("512", hotel.stay_id, False)
    assert card.status is requests_api.RequestStatus.NEW


async def test_long_summary_is_cut_to_the_card_limit(hotel: Hotel) -> None:
    request = await guest_request(hotel, summary="я" * 500)

    await _rate(hotel, request.id, DOWN)

    [card] = await feedback_cards(hotel)
    assert len(card.summary) == 500
    assert card.summary.endswith("…» — Хаускипинг")


async def test_repeat_of_the_same_rating_returns_it_without_a_second_card(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    await _rate(hotel, request.id, DOWN)

    again = await _rate(hotel, request.id, DOWN)

    assert (again.rating, again.passed_to_staff) == (DOWN, True)
    assert len(await feedback_cards(hotel)) == 1


async def test_other_rating_after_the_first_is_409(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    await _rate(hotel, request.id, DOWN)

    await expect_code(feedback_api.ERR_FEEDBACK_ALREADY_RATED, _rate(hotel, request.id, UP))


@pytest.mark.parametrize(
    "status", [requests_api.RequestStatus.NEW, requests_api.RequestStatus.IN_PROGRESS]
)
async def test_request_that_is_not_done_cannot_be_rated(
    hotel: Hotel, status: requests_api.RequestStatus
) -> None:
    request = await guest_request(hotel, status=status)

    await expect_code(feedback_api.ERR_FEEDBACK_NOT_RATEABLE, _rate(hotel, request.id, UP))


async def test_cancelled_request_cannot_be_rated(hotel: Hotel) -> None:
    request = await guest_request(hotel, status=requests_api.RequestStatus.CANCELLED)

    await expect_code(feedback_api.ERR_FEEDBACK_NOT_RATEABLE, _rate(hotel, request.id, DOWN))


async def test_request_of_another_stay_or_missing_cannot_be_rated(hotel: Hotel) -> None:
    request = await guest_request(hotel)

    await expect_code(
        feedback_api.ERR_FEEDBACK_NOT_RATEABLE,
        _rate(hotel, request.id, UP, stay_id=uuid.uuid4()),
    )
    await expect_code(feedback_api.ERR_FEEDBACK_NOT_RATEABLE, _rate(hotel, uuid.uuid4(), UP))


async def test_request_of_another_tenant_is_invisible(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    other = await make_hotel("other-hotel")

    await expect_code(
        feedback_api.ERR_FEEDBACK_NOT_RATEABLE,
        _rate(other, request.id, UP, stay_id=hotel.stay_id),
    )


async def test_switched_off_feedback_is_404(hotel: Hotel) -> None:
    request = await guest_request(hotel)
    await configure(hotel.tenant_id, enabled=False)

    await expect_code(feedback_api.ERR_FEEDBACK_DISABLED, _rate(hotel, request.id, UP))


async def test_missing_escalation_service_keeps_the_rating_without_a_card(
    hotel: Hotel, caplog: pytest.LogCaptureFixture
) -> None:
    """Ключ задан, а службы у отеля нет (удалили руками): оценка есть, карточки нет.

    Маркер утечки латиницей: JSON-лог экранирует кириллицу, и русский текст в
    логе этой проверкой не нашёлся бы.
    """
    request = await guest_request(hotel, summary="LEAKMARKER cleaning")
    await configure(hotel.tenant_id, escalation_key="front-desk")

    result = await _rate(hotel, request.id, DOWN)

    assert (result.rating, result.passed_to_staff) == (DOWN, False)
    assert await feedback_cards(hotel) == []
    assert "feedback_card_failed" in caplog.text
    assert feedback_api.ERR_FEEDBACK_CARD_FAILED in caplog.text
    assert "LEAKMARKER" not in caplog.text
    with tenant_context(hotel.tenant_id):
        states = await feedback_api.feedback_states([request.id], stay_id=hotel.stay_id)
    assert states[request.id].rating is DOWN


async def _wait_until_a_backend_blocks(*, timeout_seconds: float = 5.0) -> bool:
    """Дождаться, пока другое соединение встанет на блокировку (канон
    `tests/test_tenant_config.py`): без этого чередование выпадало бы случайно."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with platform_session_scope() as session:
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks lock_row "
                    "JOIN pg_stat_activity backend ON backend.pid = lock_row.pid "
                    "WHERE NOT lock_row.granted AND backend.datname = current_database()"
                )
            )
        if blocked:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_double_tap_on_thumbs_down_gives_one_card(
    hotel: Hotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Второе касание приходит, пока первое ещё заводит карточку: одна карточка.

    Чередование задано: первый вызов вставил оценку и стоит на карточке, второй
    к этому моменту уже упёрся в уникальный индекс. Без `ON CONFLICT DO NOTHING
    RETURNING` в одной транзакции с карточкой второй завёл бы свою.
    """
    request = await guest_request(hotel)
    create_request = requests_api.create_request
    second: list[asyncio.Task[feedback_api.FeedbackRead]] = []

    async def first_card_waits_for_the_second_tap(
        *args: Any, **kwargs: Any
    ) -> requests_api.ServiceRequestRead:
        if not second:
            second.append(asyncio.create_task(_rate(hotel, request.id, DOWN)))
            assert await _wait_until_a_backend_blocks()
        return await create_request(*args, **kwargs)

    monkeypatch.setattr(requests_api, "create_request", first_card_waits_for_the_second_tap)

    first = await _rate(hotel, request.id, DOWN)
    repeated = await second[0]

    assert (first.passed_to_staff, repeated.passed_to_staff) == (True, True)
    assert len(await feedback_cards(hotel)) == 1

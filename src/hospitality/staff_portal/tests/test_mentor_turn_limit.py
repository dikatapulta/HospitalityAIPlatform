"""Дневной лимит ходов «Наставника» на сотрудника (#966, spec 0048 §8).

Потолок отеля один на всех: без лимита один сотрудник циклом обрывов выбирал его, и
«Наставник» молчал у всего отеля. Лимит — на сотрудника: выбрал свой — ERR-MENTOR-007
до модели, а коллега спрашивает дальше.
"""

# ruff: noqa: F811

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.mentor.tests.conftest import providers
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import mentor, mentor_limit
from hospitality.staff_portal.tests.conftest import PortalHotel
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    Portal,
    _events,
    _hotel,
    _login,
    _mentor_models,
    _post,
    portal,
)
from tests.conftest import FakeRateLimitRedis

QUESTION = {"message": "Как встретить гостя?"}


async def test_employee_out_of_turns_does_not_silence_a_colleague(
    portal: Portal, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hotel(portal_hotel.tenant_id)
    counters = FakeRateLimitRedis()  # один на тест: счёт должен накапливаться
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: counters)
    monkeypatch.setattr(mentor_limit, "MENTOR_DAILY_TURNS", 1)
    fakes = providers()
    portal.answer_with(fakes)
    assert isinstance(fakes.streaming, MockLlmProvider)

    first = await _login(portal.client, portal_hotel, StaffRole.STAFF, "Әсел")
    assert _events((await _post(portal.client, "/ask", QUESTION)).text)[-1][0] == "done"
    with capture_logs() as logs:
        refused = await _post(portal.client, "/ask", QUESTION)
    assert _events(refused.text)[-1] == (
        "error",
        {"code": mentor_limit.ERR_MENTOR_TURN_LIMIT, "message": mentor.BUDGET_TEXT},
    )
    [reached] = [entry for entry in logs if entry["event"] == "mentor_turn_limit_reached"]
    assert reached["user_id"] == str(first)  # по нему находят источник (ERR-OPS-019)
    assert len(fakes.streaming.calls) == 1  # отказ — до модели

    await _login(portal.client, portal_hotel, StaffRole.STAFF, "Бауыржан")
    assert _events((await _post(portal.client, "/ask", QUESTION)).text)[-1][0] == "done"
    assert len(fakes.streaming.calls) == 2

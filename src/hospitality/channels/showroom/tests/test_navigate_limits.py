"""Лимиты помощника витрины: по адресу и по отелю, fail-open (spec 0069 §2.3 п. 4, §7 п. 5).

Счётчик — `FakeRateLimitRedis` (CI гоняет тесты без Redis). Фразы — короткие команды:
их разбирают правила, модель не нужна, и тест проверяет только лимит.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from zoneinfo import ZoneInfo

import pytest

from hospitality.ai.evals.showroom_navigate import EVAL_SCENE
from hospitality.ai.showroom.tests.conftest import hotel  # noqa: F401
from hospitality.channels.showroom import navigate as routes
from hospitality.channels.showroom.navigate import NavigateRequest, NavigateResponse
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    FakeRateLimitRedis,
    _clean_log_context,
    _frozen_ratelimit_clock,
    canonical_database,
    migrated_database_name,
)

ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture
def fake_redis() -> Iterator[FakeRateLimitRedis]:
    redis = FakeRateLimitRedis()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: redis)
        yield redis


async def _ask(tenant_id: uuid.UUID, client_ip: str) -> NavigateResponse:
    with tenant_context(tenant_id):
        return await routes.answer(
            NavigateRequest(text="этаж 15", language="ru"),
            scene=EVAL_SCENE,
            zone=ALMATY,
            assistant_enabled=False,
            client_ip=client_ip,
        )


@pytest.mark.usefixtures("fake_redis")
async def test_one_address_gets_thirty_phrases_per_ten_minutes(hotel: uuid.UUID) -> None:
    for _ in range(routes.NAVIGATE_IP_LIMIT):
        assert (await _ask(hotel, "203.0.113.7")).source == "rules"

    with pytest.raises(AppError) as error:
        await _ask(hotel, "203.0.113.7")

    assert (error.value.code, error.value.status_code) == ("ERR-SHOWROOM-003", 429)
    assert (await _ask(hotel, "198.51.100.1")).source == "rules"  # сосед — в своём окне


@pytest.mark.usefixtures("fake_redis")
async def test_hotel_day_limit_counts_every_address(hotel: uuid.UUID) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routes, "NAVIGATE_TENANT_LIMIT", 2)
        await _ask(hotel, "203.0.113.1")
        await _ask(hotel, "203.0.113.2")
        with pytest.raises(AppError) as error:
            await _ask(hotel, "203.0.113.3")

    assert error.value.code == "ERR-SHOWROOM-003"


async def test_redis_down_lets_the_visitor_through(
    hotel: uuid.UUID, fake_redis: FakeRateLimitRedis
) -> None:
    """Fail-open по канону spec 0023: деньги держит бюджет назначения, не лимит."""
    fake_redis.fail = True

    assert (await _ask(hotel, "203.0.113.7")).commands == [{"type": "focus_floor", "floor": 15}]

"""Резерв дневного бюджета и пауза между ретраями (issue #46, ADR-017, R-7).

Две дыры одного файла `service.py`: бюджет проверялся по одному только
записанному расходу (вызовы, идущие прямо сейчас, были ему невидимы), а
попытки после таймаута шли подряд без паузы. Здесь проверяется поведение,
которого до issue #46 не было: вызов в полёте держит бюджет, мёртвый резерв
его не держит, пауза растёт и не ставится после последней попытки.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from hospitality.ai.gateway import service
from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_TIMEOUT,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
)
from hospitality.ai.gateway.models import LlmBudgetReservation
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])


async def _reservations(tenant_id: uuid.UUID) -> list[LlmBudgetReservation]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmBudgetReservation)))


def _set_budget(monkeypatch: pytest.MonkeyPatch, budget_usd: Decimal) -> None:
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", str(budget_usd))
    get_settings.cache_clear()


class _BlockingProvider:
    """Провайдер, зависающий в вызове, пока тест его не отпустит.

    Так воспроизводится гонка (TOCTOU): второй вызов приходит ровно в тот
    момент, когда первый уже начат, но его стоимость ещё не записана.
    """

    name = "mock"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return LlmProviderResult(
            text="mock response", model="claude-opus-4-8", input_tokens=10, output_tokens=10
        )


class _SleepSpy:
    """Подмена модуля `asyncio` в service.py: пауза считается, но не ждётся."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


async def test_call_in_flight_holds_budget_against_parallel_call(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Главная дыра issue #46: два параллельных вызова проходили оба.

    Бюджета хватает ровно на один резерв — второй вызов обязан получить
    ERR-AI-002, пока первый ещё в полёте и в журнале его нет.
    """
    tenant_a, _ = two_tenants
    reserve_usd = service._reserve_usd(SIMPLE_REQUEST)
    _set_budget(monkeypatch, reserve_usd + reserve_usd / 2)
    provider = _BlockingProvider()

    with tenant_context(tenant_a):
        first = asyncio.create_task(complete(SIMPLE_REQUEST, provider=provider))
        await provider.started.wait()

        # Второму вызову — свой, не блокирующий провайдер: иначе тест без
        # починки не падал бы, а висел на общем `release` (проверено снятием
        # починки — прогон уходил в таймаут вместо красного).
        parallel_provider = MockLlmProvider()
        with pytest.raises(AppError) as error:
            await complete(SIMPLE_REQUEST, provider=parallel_provider)
        assert error.value.code == ERR_AI_BUDGET_EXCEEDED
        assert parallel_provider.calls == []  # отказ не дошёл до провайдера

        provider.release.set()
        response = await first

    assert response.text == "mock response"
    # Отвергнутый вызов снял свой резерв, успешный — свой: чужой бюджет
    # не держит ни тот, ни другой.
    assert await _reservations(tenant_a) == []


async def test_expired_reservation_does_not_hold_budget_and_is_swept(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Процесс, умерший между резервом и записью исхода, не запирает отель до полуночи."""
    tenant_a, _ = two_tenants
    dead = LlmBudgetReservation(
        amount_usd=Decimal("1000"), reserved_until=utc_now() - timedelta(seconds=1)
    )
    with tenant_context(tenant_a):
        async with session_scope() as session:
            session.add(dead)

    with tenant_context(tenant_a):
        response = await complete(SIMPLE_REQUEST, provider=MockLlmProvider())

    assert response.text == "mock response"
    # Истёкшая строка не только не считается, но и удалена следующим резервом.
    assert await _reservations(tenant_a) == []


async def test_live_reservation_of_another_tenant_is_invisible(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Резерв — тенантная таблица под RLS (P-4): сосед по инсталляции не мешает."""
    tenant_a, tenant_b = two_tenants
    reserve_usd = service._reserve_usd(SIMPLE_REQUEST)
    _set_budget(monkeypatch, reserve_usd + reserve_usd / 2)
    provider = _BlockingProvider()

    with tenant_context(tenant_a):
        first = asyncio.create_task(complete(SIMPLE_REQUEST, provider=provider))
        await provider.started.wait()

    with tenant_context(tenant_b):
        # Тот же бюджет, но свой: вызов тенанта B проходит.
        second = await complete(SIMPLE_REQUEST, provider=MockLlmProvider())
    assert second.text == "mock response"

    provider.release.set()
    await first


async def test_pause_grows_between_retries_and_is_not_taken_after_the_last(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пауза по канону ADR-009 (база × 2^(попытка−1)) с половинным разбросом.

    После последней попытки паузы нет: гость и так уже ждёт весь таймаут,
    ждать ещё и «на дорожку» перед отказом ему незачем.
    """
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "2.0")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "3")
    # Таймаут пинится не зря: он служит потолком паузы, и ассерты ниже верны
    # только пока он не ниже 4 секунд.
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30.0")
    get_settings.cache_clear()
    spy = _SleepSpy()
    monkeypatch.setattr(service, "asyncio", spy)

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(SIMPLE_REQUEST, provider=MockLlmProvider(timeouts_before_success=100))

    assert error.value.code == ERR_AI_PROVIDER_TIMEOUT
    assert len(spy.delays) == 2  # три попытки — две паузы между ними
    assert 1.0 <= spy.delays[0] <= 2.0  # 2 с в разбросе [0.5×, 1×]
    assert 2.0 <= spy.delays[1] <= 4.0  # 4 с там же


def test_pause_never_exceeds_one_attempt_timeout() -> None:
    """Потолок паузы — таймаут одной попытки, а не отдельная настройка."""
    settings = Settings(llm_retry_backoff_base_seconds=10.0, llm_timeout_seconds=30.0)

    assert service._retry_delay_seconds(1, settings) == 10.0
    assert service._retry_delay_seconds(2, settings) == 20.0
    assert service._retry_delay_seconds(3, settings) == 30.0  # 40 → потолок
    assert service._retry_delay_seconds(9, settings) == 30.0

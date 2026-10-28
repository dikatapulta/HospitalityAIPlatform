"""Назначение вызова: своя модель и свой бюджет (ADR-024 §1–2, §7; spec 0048 §11, PR 2).

Главное, что здесь проверяется: обучение персонала не съедает бюджет гостя, и
наоборот; выключенное назначение не доходит до провайдера; метрики и алерты
гостя не видят расхода наставника; процесс не стартует с негодной моделью
наставника.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from hospitality.ai.gateway import service
from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
    refresh_budget_metrics,
    refresh_call_outcome_metrics,
    validate_configured_model,
)
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.price_list import MODEL_PRICING_USD_PER_MTOK
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderResult
from hospitality.ai.gateway.providers import provider_for_model
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

MENTOR_MODEL = "gpt-6-luna"
GUEST_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])
MENTOR_REQUEST = LlmRequest(
    messages=[LlmMessage(role="user", content="Как встречать гостя?")], purpose="mentor"
)


@pytest.fixture
def mentor_enabled() -> Iterator[None]:
    """Назначение `mentor` включено. Свой `MonkeyPatch.context()`, а не общий
    `monkeypatch`: иначе откат окружения зависел бы от порядка фикстур, и временная
    БД `canonical_database` удалялась бы раньше отката `POSTGRES_DB` (tests/conftest.py)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL", "claude-sonnet-5")
        patch.setenv("LLM_MODEL_MENTOR", MENTOR_MODEL)
        patch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
        patch.setenv("OPENAI_API_KEY", "sk-test")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _set_budgets(monkeypatch: pytest.MonkeyPatch, *, guest: Decimal, mentor: Decimal) -> None:
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", str(guest))
    monkeypatch.setenv("LLM_MENTOR_DAILY_BUDGET_USD", str(mentor))
    get_settings.cache_clear()


async def _rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at)))


@pytest.mark.usefixtures("canonical_database", "mentor_enabled")
async def test_mentor_call_is_logged_under_its_purpose_and_model(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(model=MENTOR_MODEL, input_tokens=10_000, output_tokens=400)

    with tenant_context(tenant_a):
        response = await complete(MENTOR_REQUEST, provider=provider)

    # 10K × $0,10 + 400 × $0,50 за 1M = $0,0012 — число spec 0048 §8.
    assert response.cost_usd == Decimal("0.0012")
    [row] = await _rows(tenant_a)
    assert row.purpose == "mentor"
    assert row.model == MENTOR_MODEL


@pytest.mark.usefixtures("canonical_database", "mentor_enabled")
async def test_spent_mentor_budget_does_not_touch_guest_and_back(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исчерпанный бюджет одного назначения другое не останавливает (ADR-024 §1)."""
    tenant_a, _ = two_tenants
    # Каждый бюджет — с запасом на один свой резерв, но меньше одного вызова за $2.
    _set_budgets(monkeypatch, guest=Decimal("1"), mentor=Decimal("1"))
    expensive = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)  # $2 на Opus

    with tenant_context(tenant_a):
        await complete(MENTOR_REQUEST, provider=expensive)  # наставник ушёл за лимит
        with pytest.raises(AppError) as error:
            await complete(MENTOR_REQUEST, provider=MockLlmProvider(model=MENTOR_MODEL))
        assert error.value.code == ERR_AI_BUDGET_EXCEEDED

        guest = await complete(GUEST_REQUEST, provider=MockLlmProvider())  # гость отвечает
        assert guest.text == "mock response"

        await complete(GUEST_REQUEST, provider=expensive)  # теперь гость за лимитом
        with pytest.raises(AppError):
            await complete(GUEST_REQUEST, provider=MockLlmProvider())

    statuses = [(row.purpose, row.status) for row in await _rows(tenant_a)]
    assert statuses == [
        ("mentor", LlmCallStatus.OK),
        ("mentor", LlmCallStatus.BUDGET_EXCEEDED),
        ("guest", LlmCallStatus.OK),
        ("guest", LlmCallStatus.OK),
        ("guest", LlmCallStatus.BUDGET_EXCEEDED),
    ]


@pytest.mark.usefixtures("canonical_database", "mentor_enabled")
async def test_live_reservation_holds_only_its_purpose(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Резерв в полёте считает своё назначение: огромный резерв наставника гостю не мешает."""
    tenant_a, _ = two_tenants
    _set_budgets(monkeypatch, guest=Decimal("1"), mentor=Decimal("1"))
    with tenant_context(tenant_a):
        async with session_scope() as session:
            session.add(
                LlmBudgetReservation(
                    amount_usd=Decimal("1000"),
                    purpose="mentor",
                    reserved_until=utc_now() + timedelta(minutes=5),
                )
            )
        response = await complete(GUEST_REQUEST, provider=MockLlmProvider())
    assert response.text == "mock response"


def test_reserve_is_priced_by_the_purpose_model(mentor_enabled: None) -> None:
    """Резерв наставника — по цене `gpt-6-luna`, а не гостевого Sonnet 5 (ревью PR #499, Н-2)."""
    mentor_reserve = service._reserve_usd(MENTOR_REQUEST)
    guest_price = MODEL_PRICING_USD_PER_MTOK["claude-sonnet-5"]
    mentor_price = MODEL_PRICING_USD_PER_MTOK[MENTOR_MODEL]
    chars = len(service._request_payload(MENTOR_REQUEST))

    expected = service._reserve_amount(
        chars=chars,
        input_price=mentor_price[0],
        output_tokens=MENTOR_REQUEST.max_tokens,
        output_price=mentor_price[1],
    )
    assert mentor_reserve == expected
    guest_priced = service._reserve_amount(
        chars=chars,
        input_price=guest_price[0],
        output_tokens=MENTOR_REQUEST.max_tokens,
        output_price=guest_price[1],
    )
    assert mentor_reserve * 10 < guest_priced


def test_purpose_is_not_part_of_prompt_hash() -> None:
    same_text = MENTOR_REQUEST.model_copy(update={"purpose": "guest"})
    assert service.compute_prompt_hash(MENTOR_REQUEST) == service.compute_prompt_hash(same_text)


@pytest.mark.usefixtures("canonical_database")
async def test_disabled_purpose_is_refused_before_provider_and_log(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой `LLM_MODEL_MENTOR` — ERR-AI-006: провайдер не зовётся, строки нет (ADR-024 §2)."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MODEL_MENTOR", "")
    get_settings.cache_clear()
    provider = MockLlmProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(MENTOR_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert error.value.status_code == 503
    assert provider.calls == []
    assert await _rows(tenant_a) == []


@pytest.mark.usefixtures("canonical_database", "mentor_enabled")
async def test_guest_metrics_do_not_see_mentor_calls(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Метрики алертов гостя — только `guest` (ADR-024 §7): сбой наставника не будит основателя."""
    tenant_a, _ = two_tenants

    class _Failing:
        name = "mock"

        async def complete(self, request: LlmRequest) -> LlmProviderResult:
            raise LlmProviderError("mentor provider is down")

    expensive = MockLlmProvider(model=MENTOR_MODEL, input_tokens=1_000_000, output_tokens=0)
    with tenant_context(tenant_a):
        await complete(MENTOR_REQUEST, provider=expensive)  # $0,10 расхода наставника
        for _ in range(3):
            with pytest.raises(AppError):
                await complete(MENTOR_REQUEST, provider=_Failing())

    await refresh_budget_metrics()
    await refresh_call_outcome_metrics()

    labels = {"tenant_id": str(tenant_a)}
    assert REGISTRY.get_sample_value("llm_daily_spend_usd", labels) == 0.0
    assert (
        REGISTRY.get_sample_value("llm_provider_failure_streak", labels | {"status": "error"})
        == 0.0
    )


def test_default_provider_is_built_for_the_purpose_model(mentor_enabled: None) -> None:
    provider_for_model.cache_clear()
    try:
        assert provider_for_model(MENTOR_MODEL).name == "openai"
    finally:
        provider_for_model.cache_clear()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"LLM_MODEL_MENTOR": "gpt-unknown"}, "LLM_MODEL_MENTOR"),
        ({"LLM_MODEL_MENTOR": "claude-haiku-4-5"}, "поток"),
        ({"OPENAI_API_KEY": ""}, "OPENAI_API_KEY"),
        ({"LLM_EMBEDDING_MODEL": "text-embedding-unknown"}, "LLM_EMBEDDING_MODEL"),
    ],
)
def test_startup_rejects_unusable_mentor_configuration(
    mentor_enabled: None, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Fail-fast старта (ADR-024 §2): модель в прайс-листе, умеет поток, ключ задан."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as error:
        validate_configured_model()
    assert expected in str(error.value)


def test_startup_passes_with_usable_mentor_configuration(mentor_enabled: None) -> None:
    validate_configured_model()  # не бросает — иначе тест упал бы здесь


def test_startup_ignores_embedding_model_while_mentor_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Назначение выключено — dev и CI поднимаются без ключа OpenAI (ADR-024 §2)."""
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("LLM_MODEL_MENTOR", "")
    monkeypatch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-unknown")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        validate_configured_model()
    finally:
        get_settings.cache_clear()

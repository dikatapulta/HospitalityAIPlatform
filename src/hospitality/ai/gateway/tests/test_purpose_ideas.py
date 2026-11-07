"""Назначение `ideas` — «Предложить идею» и интервью служб (spec 0071 §7, §12 PR 2).

Свой кошелёк: исчерпанный бюджет идей не трогает гостя и «Наставника», и наоборот;
пустая модель — ERR-AI-006 до провайдера; процесс не стартует с моделью вне
прайс-листа или без ключа её провайдера.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    LlmMessage,
    LlmPurpose,
    LlmRequest,
    MockLlmProvider,
    complete,
    validate_configured_model,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.purposes import purpose_settings
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

IDEAS_MODEL = "claude-sonnet-5"
MENTOR_MODEL = "gpt-6-luna"


def _request(purpose: LlmPurpose, text: str) -> LlmRequest:
    return LlmRequest(messages=[LlmMessage(role="user", content=text)], purpose=purpose)


IDEAS_REQUEST = _request("ideas", "Пусть бот сам ставит уборку после выезда")
MENTOR_REQUEST = _request("mentor", "Как встречать гостя?")
GUEST_REQUEST = _request("guest", "Привет!")


@pytest.fixture
def ideas_enabled() -> Iterator[None]:
    """Назначения `ideas` и `mentor` включены, ключи заданы (свой `MonkeyPatch.context()` —
    по той же причине, что фикстура `mentor_enabled` в `test_purposes.py`)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL", "claude-sonnet-5")
        patch.setenv("LLM_MODEL_IDEAS", IDEAS_MODEL)
        patch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        patch.setenv("LLM_MODEL_MENTOR", MENTOR_MODEL)
        patch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
        patch.setenv("OPENAI_API_KEY", "sk-test")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def _rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at)))


def test_ideas_purpose_has_its_own_model_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL_IDEAS", IDEAS_MODEL)
    monkeypatch.setenv("LLM_IDEAS_DAILY_BUDGET_USD", "0.5")
    get_settings.cache_clear()
    try:
        config = purpose_settings("ideas", get_settings())
    finally:
        get_settings.cache_clear()
    assert (config.model, config.daily_budget_usd) == (IDEAS_MODEL, Decimal("0.5"))


def test_ideas_budget_default_is_two_dollars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потолок spec 0071 §7 — $2 в день, отдельно от $10 гостя и $2 «Наставника»."""
    monkeypatch.delenv("LLM_IDEAS_DAILY_BUDGET_USD", raising=False)
    get_settings.cache_clear()
    try:
        assert purpose_settings("ideas", get_settings()).daily_budget_usd == Decimal("2.0")
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("canonical_database", "ideas_enabled")
async def test_spent_ideas_budget_does_not_touch_guest_and_mentor(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Интервью съели свой потолок — гость и «Наставник» отвечают (§7, §12)."""
    tenant_a, _ = two_tenants
    for name in (
        "LLM_IDEAS_DAILY_BUDGET_USD",
        "LLM_TENANT_DAILY_BUDGET_USD",
        "LLM_MENTOR_DAILY_BUDGET_USD",
    ):
        monkeypatch.setenv(name, "1")
    get_settings.cache_clear()
    expensive = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)  # $2 на Opus

    with tenant_context(tenant_a):
        await complete(IDEAS_REQUEST, provider=expensive)  # идеи ушли за лимит
        with pytest.raises(AppError) as error:
            await complete(IDEAS_REQUEST, provider=MockLlmProvider(model=IDEAS_MODEL))
        assert error.value.code == ERR_AI_BUDGET_EXCEEDED

        guest = await complete(GUEST_REQUEST, provider=MockLlmProvider())
        mentor = await complete(MENTOR_REQUEST, provider=MockLlmProvider(model=MENTOR_MODEL))
    assert (guest.text, mentor.text) == ("mock response", "mock response")

    statuses = [(row.purpose, row.status) for row in await _rows(tenant_a)]
    assert statuses == [
        ("ideas", LlmCallStatus.OK),
        ("ideas", LlmCallStatus.BUDGET_EXCEEDED),
        ("guest", LlmCallStatus.OK),
        ("mentor", LlmCallStatus.OK),
    ]


@pytest.mark.usefixtures("canonical_database", "ideas_enabled")
async def test_spent_guest_budget_does_not_stop_ideas(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "1")
    get_settings.cache_clear()

    with tenant_context(tenant_a):
        await complete(GUEST_REQUEST, provider=MockLlmProvider(input_tokens=400_000))  # $2
        with pytest.raises(AppError):
            await complete(GUEST_REQUEST, provider=MockLlmProvider())
        idea = await complete(IDEAS_REQUEST, provider=MockLlmProvider(model=IDEAS_MODEL))
    assert idea.text == "mock response"


@pytest.mark.usefixtures("canonical_database")
async def test_disabled_ideas_purpose_is_refused_before_provider_and_log(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой `LLM_MODEL_IDEAS` — ERR-AI-006: провайдер не зовётся, строки журнала нет."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MODEL_IDEAS", "")
    get_settings.cache_clear()
    provider = MockLlmProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(IDEAS_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert provider.calls == []
    assert await _rows(tenant_a) == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"LLM_MODEL_IDEAS": "claude-sonnet-5-20260101"}, "LLM_MODEL_IDEAS"),
        ({"ANTHROPIC_API_KEY": ""}, "ANTHROPIC_API_KEY"),
        ({"LLM_MODEL_IDEAS": "gpt-6-luna", "OPENAI_API_KEY": ""}, "OPENAI_API_KEY"),
    ],
)
def test_startup_rejects_unusable_ideas_configuration(
    ideas_enabled: None, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Fail-fast старта (spec 0071 §7): модель в прайс-листе и ключ её провайдера."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as error:
        validate_configured_model()
    assert expected in str(error.value)


def test_startup_passes_with_usable_ideas_configuration(ideas_enabled: None) -> None:
    validate_configured_model()  # не бросает — иначе тест упал бы здесь


def test_startup_ignores_missing_anthropic_key_while_ideas_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Раздел выключен — dev и CI поднимаются без ключа Anthropic, как раньше."""
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("LLM_MODEL_IDEAS", "")
    monkeypatch.setenv("LLM_MODEL_MENTOR", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        validate_configured_model()
    finally:
        get_settings.cache_clear()


def test_compose_passes_ideas_settings_to_app_and_worker() -> None:
    """Без проброса строка .env не дошла бы до контейнера (класс #486 п. 3)."""
    compose = (
        Path(__file__).parents[5] / "ops" / "deploy" / "docker-compose.staging.yml"
    ).read_text("utf-8")
    assert compose.count("LLM_MODEL_IDEAS: ${LLM_MODEL_IDEAS:-}") == 2
    assert compose.count("LLM_IDEAS_DAILY_BUDGET_USD: ${LLM_IDEAS_DAILY_BUDGET_USD:-2.0}") == 2

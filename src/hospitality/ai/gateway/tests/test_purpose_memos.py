"""Назначение `memos` — «Оформить» служебной записки (spec 0077 §7, §12 PR 6).

Свой кошелёк: исчерпанный бюджет оформлений не трогает гостя, и наоборот; пустая модель —
ERR-AI-006 до провайдера; процесс не стартует с моделью вне прайс-листа или без ключа
её провайдера.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

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

MEMOS_MODEL = "claude-sonnet-5"


def _request(purpose: LlmPurpose, text: str) -> LlmRequest:
    return LlmRequest(messages=[LlmMessage(role="user", content=text)], purpose=purpose)


MEMOS_REQUEST = _request("memos", "<text>\nв 412 течет кран\n</text>")
GUEST_REQUEST = _request("guest", "Привет!")


@pytest.fixture
def memos_enabled() -> Iterator[None]:
    """Назначение `memos` включено, ключ задан (свой `MonkeyPatch.context()` — как у
    фикстуры `ideas_enabled` в `test_purpose_ideas.py`)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL", "claude-sonnet-5")
        patch.setenv("LLM_MODEL_MEMOS", MEMOS_MODEL)
        patch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        patch.setenv("LLM_MODEL_MENTOR", "")
        patch.setenv("LLM_MODEL_IDEAS", "")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def _rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at)))


def test_memos_budget_default_is_one_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потолок spec 0077 §7 — $1 в день; модель — из `LLM_MODEL_MEMOS`."""
    monkeypatch.setenv("LLM_MODEL_MEMOS", MEMOS_MODEL)
    monkeypatch.delenv("LLM_MEMOS_DAILY_BUDGET_USD", raising=False)
    get_settings.cache_clear()
    try:
        config = purpose_settings("memos", get_settings())
    finally:
        get_settings.cache_clear()
    assert (config.model, config.daily_budget_usd) == (MEMOS_MODEL, Decimal("1.0"))


@pytest.mark.usefixtures("canonical_database", "memos_enabled")
async def test_spent_memos_budget_does_not_touch_guest(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Оформления съели свой потолок — гость отвечает, и наоборот (§12)."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MEMOS_DAILY_BUDGET_USD", "1")
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "1")
    get_settings.cache_clear()
    expensive = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)

    with tenant_context(tenant_a):
        await complete(MEMOS_REQUEST, provider=expensive)
        with pytest.raises(AppError) as error:
            await complete(MEMOS_REQUEST, provider=MockLlmProvider(model=MEMOS_MODEL))
        assert error.value.code == ERR_AI_BUDGET_EXCEEDED
        guest = await complete(GUEST_REQUEST, provider=MockLlmProvider())
    assert guest.text == "mock response"

    statuses = [(row.purpose, row.status) for row in await _rows(tenant_a)]
    assert statuses == [
        ("memos", LlmCallStatus.OK),
        ("memos", LlmCallStatus.BUDGET_EXCEEDED),
        ("guest", LlmCallStatus.OK),
    ]


@pytest.mark.usefixtures("canonical_database")
async def test_disabled_memos_purpose_is_refused_before_provider_and_log(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MODEL_MEMOS", "")
    get_settings.cache_clear()
    provider = MockLlmProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(MEMOS_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert provider.calls == []
    assert await _rows(tenant_a) == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"LLM_MODEL_MEMOS": "claude-sonnet-5-20260101"}, "LLM_MODEL_MEMOS"),
        ({"ANTHROPIC_API_KEY": ""}, "LLM_MODEL_MEMOS"),
    ],
)
def test_startup_rejects_unusable_memos_configuration(
    memos_enabled: None, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Fail-fast старта (spec 0077 §7): модель в прайс-листе и ключ её провайдера."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as error:
        validate_configured_model()
    assert expected in str(error.value)


def test_startup_passes_with_usable_memos_configuration(memos_enabled: None) -> None:
    validate_configured_model()  # не бросает — иначе тест упал бы здесь

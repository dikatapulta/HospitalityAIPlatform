"""Ответ целиком (#236, аудит 05.08 В-6): обрыв лимитом и отказ модели — не успех.

До #236 шлюз отдавал оборванный `max_tokens` ответ как обычный: гость получал
реплику на полуслове, а оборванный вызов инструмента — неполные аргументы.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    ERR_AI_INCOMPLETE_ANSWER,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    MockTurn,
    ScriptedLlmProvider,
    complete,
    complete_whole,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.test_logging import find_record, read_log_records

REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="расскажи про завтрак")])


async def _statuses(tenant_id: uuid.UUID) -> list[LlmCallStatus]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog.status)))


async def test_truncated_answer_is_retried_with_double_limit_and_cost_summed(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = ScriptedLlmProvider(
        [MockTurn(text="Завтрак с 7:00 до", stop_reason="max_tokens"), MockTurn(text="Целиком.")]
    )

    with tenant_context(tenant_a):
        response = await complete_whole(REQUEST, provider=provider)
        single = await complete(REQUEST, provider=MockLlmProvider())

    assert response.text == "Целиком."
    assert [call.max_tokens for call in provider.calls] == [1024, 2048]
    # Учёт — оба вызова: лимит чата (spec 0023 §5) складывает цену хода.
    assert response.cost_usd == single.cost_usd * 2
    assert response.input_tokens == 400
    assert response.output_tokens == 200
    assert await _statuses(tenant_a) == [LlmCallStatus.OK] * 3


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
async def test_still_truncated_after_retry_gives_err_ai_007(
    two_tenants: tuple[uuid.UUID, uuid.UUID], stop_reason: str
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(text="обрыв на полу", stop_reason=stop_reason)

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete_whole(REQUEST, provider=provider)

    assert raised.value.code == ERR_AI_INCOMPLETE_ANSWER
    assert len(provider.calls) == 2


@pytest.mark.parametrize("stop_reason", ["refusal", "content_filter"])
async def test_refusal_is_not_retried_and_gives_err_ai_007(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    stop_reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(text="", stop_reason=stop_reason)
    configure_logging()

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete_whole(REQUEST, provider=provider)

    assert raised.value.code == ERR_AI_INCOMPLETE_ANSWER
    assert len(provider.calls) == 1
    records = read_log_records(capsys)
    assert find_record(records, "llm_answer_incomplete")["stop_reason"] == stop_reason
    # Причина остановки — и в строке каждого вызова, у любого назначения.
    assert find_record(records, "llm_call")["stop_reason"] == stop_reason


async def test_whole_answer_passes_through_with_one_call(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(text="Завтрак с 7:00 до 10:00.")

    with tenant_context(tenant_a):
        response = await complete_whole(REQUEST, provider=provider)

    assert response.text == "Завтрак с 7:00 до 10:00."
    assert response.stop_reason == "end_turn"
    assert len(provider.calls) == 1
    assert response.cost_usd > Decimal(0)

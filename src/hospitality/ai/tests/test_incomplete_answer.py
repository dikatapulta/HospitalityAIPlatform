"""Неполный ответ модели в ходе гостя (#236, аудит 05.08 В-6).

Оборванный лимитом вызов инструмента приходит правдоподобно неполным: SDK
разбирает обрывок JSON в валидный объект. Исполнять его или ставить в гейт P-9
нельзя — оркестратор повторяет вызов шлюза с бо́льшим лимитом, а если и это не
помогло, отдаёт ERR-AI-007 каналу (деградация §7.8).
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import (
    ERR_AI_INCOMPLETE_ANSWER,
    LlmMessage,
    MockTurn,
    ScriptedLlmProvider,
    ToolCall,
)
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


def _request_call(summary: str) -> ToolCall:
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={"category_key": "housekeeping", "summary": summary, "room_number": "305"},
    )


async def test_truncated_tool_call_is_replaced_by_the_whole_one(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_request_call("убрать номер 3")], stop_reason="max_tokens"),
            MockTurn(tool_calls=[_request_call("убрать номер 305 и сменить полотенца")]),
        ]
    )

    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="уберите номер", provider=provider)

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action is not None
    assert turn.pending_action.arguments["summary"] == "убрать номер 305 и сменить полотенца"
    assert [call.max_tokens for call in provider.calls] == [1024, 2048]


async def test_confirm_verdict_cut_twice_executes_nothing(demo_tenant: uuid.UUID) -> None:
    verdict = ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": "Готово, перед"},
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[verdict], stop_reason="max_tokens")] * 2)

    with tenant_context(demo_tenant):
        with pytest.raises(AppError) as raised:
            await orchestrator.handle_message(
                message="да",
                history=[LlmMessage(role="assistant", content="Оформить уборку номера 305?")],
                pending_action=PendingAction(
                    tool_name="create_service_request",
                    arguments=_request_call("убрать номер 305").arguments,
                ),
                provider=provider,
            )
        page = await requests_api.list_requests(limit=1, offset=0)

    assert raised.value.code == ERR_AI_INCOMPLETE_ANSWER
    assert page.total == 0


async def test_refusal_on_the_guest_turn_reaches_the_channel(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(text="", stop_reason="refusal")])

    with tenant_context(demo_tenant), pytest.raises(AppError) as raised:
        await orchestrator.handle_message(message="есть ли утюг?", provider=provider)

    assert raised.value.code == ERR_AI_INCOMPLETE_ANSWER
    assert len(provider.calls) == 1

"""Потолок хода в оркестраторе (issue #271, spec 0055): один на весь ход.

Ход подтверждения, на котором гость передумал, зовёт шлюз дважды —
классификатор, затем новый запрос. Свежий потолок на каждый вызов удвоил бы
худшее ожидание гостя; второй вызов обязан получить остаток того же.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.gateway import api as gateway_api
from hospitality.ai.gateway.api import (
    Deadline,
    LlmRequest,
    LlmResponse,
    MockTurn,
    ScriptedLlmProvider,
    ToolCall,
)
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.shared.tenancy import tenant_context


async def test_confirmation_turn_shares_one_deadline_across_both_calls(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Deadline | None] = []
    real_complete = gateway_api.complete

    async def recording_complete(request: LlmRequest, **kwargs: Any) -> LlmResponse:
        seen.append(kwargs.get("deadline"))
        return await real_complete(request, **kwargs)

    monkeypatch.setattr(gateway_api, "complete", recording_complete)
    verdict = ToolCall(
        id="toolu_verdict", name="resolve_confirmation", arguments={"decision": "other"}
    )
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[verdict]), MockTurn(text="Что ещё подсказать?")]
    )
    deadline = Deadline.after(25.0)

    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="а во сколько завтрак?",
            pending_action=PendingAction(
                tool_name="create_service_request",
                arguments={
                    "category_key": "housekeeping",
                    "summary": "уборка",
                    "room_number": "305",
                },
            ),
            provider=provider,
            deadline=deadline,
        )

    assert turn.kind is TurnKind.REPLY
    assert len(seen) == 2
    assert all(item is deadline for item in seen)

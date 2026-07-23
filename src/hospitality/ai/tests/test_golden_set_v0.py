"""Golden-set v0 оркестратора на mock (Task 0015, §7.7 — заготовка).

Три канонических сценария на Fake-провайдере: детерминированный CI-гейт пути
«текст → LLM(mock) → инструмент → сервис requests → заявка», подтверждение и
отказ. Язык здесь не значим (mock не судит содержание) — качество на 6 языках
проверяет офлайн bake-off на реальных моделях (`ai/evals/bakeoff.py`, §7.7).
"""

from __future__ import annotations

import uuid

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import TurnKind
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


async def _request_total() -> int:
    page = await requests_api.list_requests(limit=1, offset=0)
    return page.total


_CLEANING_CALL = ToolCall(
    id="toolu_1",
    name="create_service_request",
    arguments={
        "category_key": "housekeeping",
        "summary": "уборка номера 305",
        "room_number": "305",
        # Вопрос-подтверждение гостю — аргумент инструмента на его языке (P-9).
        "confirmation_question": "Оформить уборку номера 305?",
    },
)


def _confirmation_verdict(decision: str, reply: str = "") -> ToolCall:
    """Вердикт классификатора хода подтверждения (гейт P-9, Task 0017.1)."""
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": decision, "reply": reply},
    )


async def test_golden_v0_1_service_request_created_after_confirmation(
    demo_tenant: uuid.UUID,
) -> None:
    """«уберите номер» → предложение → подтверждение гостя → заявка в БД."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305?", tool_calls=[_CLEANING_CALL]),
            MockTurn(tool_calls=[_confirmation_verdict("confirm", "Готово, передаю в службу.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
        assert proposal.pending_action is not None
        assert await _request_total() == 0  # до подтверждения заявки нет (P-9)

        history = [
            LlmMessage(role="user", content="уберите номер 305"),
            LlmMessage(role="assistant", content=proposal.reply_text),
        ]
        done = await orchestrator.handle_message(
            message="да, оформляйте",
            history=history,
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert done.kind is TurnKind.ACTION_DONE
        assert done.created_request_id is not None
        assert await _request_total() == 1


async def test_golden_v0_2_question_gets_text_reply_without_tool(demo_tenant: uuid.UUID) -> None:
    """Вопрос без действия → текстовый ответ, инструмент не вызван, заявок нет."""
    provider = ScriptedLlmProvider([MockTurn(text="Завтрак с 07:00 до 10:00.")])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="во сколько завтрак?", provider=provider)
        assert turn.kind is TurnKind.REPLY
        assert turn.reply_text == "Завтрак с 07:00 до 10:00."
        assert await _request_total() == 0


async def test_golden_v0_3_no_request_when_guest_declines(demo_tenant: uuid.UUID) -> None:
    """Предложение → гость не подтвердил → заявка не создаётся (гейт P-9)."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305?", tool_calls=[_CLEANING_CALL]),
            MockTurn(tool_calls=[_confirmation_verdict("decline", "Хорошо, не оформляю.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION

        declined = await orchestrator.handle_message(
            message="нет, не надо",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert declined.kind is TurnKind.REPLY
        assert await _request_total() == 0

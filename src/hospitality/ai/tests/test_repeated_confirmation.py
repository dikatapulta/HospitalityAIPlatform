"""Гейт P-9 не переспрашивает тот же вопрос после «да» (issue #858).

Модель спросила «Оформить заявку…?» свободным текстом, гость ответил «да»,
модель на этом ходу зовёт инструмент — второй раз тот же вопрос гость не
получает. Признак — строгий: прямо перед «да» в истории ровно этот вопрос,
и классификатор гейта (`resolve_confirmation`) вернул `confirm`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.ai.repeated_confirmation import asked_just_before
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

QUESTION = "Оформить заявку на доставку туалетной бумаги в номер 1505?"


def _request_call() -> ToolCall:
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "туалетная бумага в 1505",
            "room_number": "1505",
            "confirmation_question": QUESTION,
        },
    )


def _verdict(decision: str, reply: str = "") -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": decision, "reply": reply},
    )


def _history(bot_text: str) -> list[LlmMessage]:
    return [
        LlmMessage(role="user", content="принесите туалетную бумагу в 1505"),
        LlmMessage(role="assistant", content=bot_text),
    ]


async def _request_total() -> int:
    return (await requests_api.list_requests(limit=1, offset=0)).total


async def test_same_question_before_yes_executes_without_asking_again(
    demo_tenant: uuid.UUID,
) -> None:
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_request_call()]),
            MockTurn(tool_calls=[_verdict("confirm", "Готово, передаю в службу.")]),
        ]
    )
    # Текст бота отличается от вопроса гейта только регистром, пробелами и «?».
    bot_text = "  оформить заявку на доставку  туалетной бумаги в номер 1505 ?"
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="да", history=_history(bot_text), provider=provider
        )
        assert turn.kind is TurnKind.ACTION_DONE
        assert turn.pending_action is None
        assert turn.created_request_id is not None
        assert turn.reply_text == "Готово, передаю в службу."
        assert await _request_total() == 1
    # Тот же классификатор, что на ходе с `pending_action`, и цена обоих вызовов
    # (вызов фейка — $0,0035, как в test_orchestrator.py).
    assert provider.calls[1].forced_tool == "resolve_confirmation"
    assert provider.calls[1].messages[-1] == LlmMessage(role="user", content="да")
    assert turn.llm_cost_usd == Decimal("0.0070")


async def test_other_bot_text_before_yes_still_asks(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_request_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="да",
            history=_history("Могу принести бумагу. Оформить заявку?"),
            provider=provider,
        )
        assert turn.kind is TurnKind.AWAITING_CONFIRMATION
        assert turn.reply_text == QUESTION
        assert await _request_total() == 0
    assert len(provider.calls) == 1  # классификатор не звали


async def test_same_question_but_no_or_a_question_still_asks(demo_tenant: uuid.UUID) -> None:
    for guest_reply, decision in (("нет", "decline"), ("а сколько стоит?", "other")):
        provider = ScriptedLlmProvider(
            [MockTurn(tool_calls=[_request_call()]), MockTurn(tool_calls=[_verdict(decision)])]
        )
        with tenant_context(demo_tenant):
            turn = await orchestrator.handle_message(
                message=guest_reply, history=_history(QUESTION), provider=provider
            )
            assert turn.kind is TurnKind.AWAITING_CONFIRMATION
            assert turn.reply_text == QUESTION
            assert turn.pending_action == PendingAction(
                tool_name="create_service_request", arguments=_request_call().arguments
            )
            assert await _request_total() == 0


async def test_first_message_of_dialog_asks(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_request_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="да", provider=provider)
        assert turn.kind is TurnKind.AWAITING_CONFIRMATION
        assert await _request_total() == 0
    assert len(provider.calls) == 1


async def test_confirm_staff_class_is_not_touched(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registry, "confirmation_class", lambda tool_name: ConfirmationClass.CONFIRM_STAFF
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_request_call()])])
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(
            message="да", history=_history(QUESTION), provider=provider
        )
    # Для `confirm_staff` проверка повтора не включается — классификатор не звали.
    assert len(provider.calls) == 1


def test_guest_message_last_in_history_is_not_a_repeat() -> None:
    history = [
        LlmMessage(role="assistant", content=QUESTION),
        LlmMessage(role="user", content="да"),
    ]
    assert not asked_just_before(history, QUESTION)
    assert not asked_just_before([LlmMessage(role="assistant", content="?")], "?")

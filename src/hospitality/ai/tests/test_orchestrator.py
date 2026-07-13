"""Тесты оркестратора на Fake-провайдере (Task 0015, R-7).

Краевые случаи поверх golden-set v0 (test_golden_set_v0.py): эскалация при
нарушении контракта инструмента и резервный текст подтверждения. Полный путь
«текст → LLM(mock) → инструмент → сервис requests → заявка» — в golden-set.
"""

from __future__ import annotations

import uuid

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


async def _request_total() -> int:
    page = await requests_api.list_requests(limit=1, offset=0)
    return page.total


def _housekeeping_call(category_key: str = "housekeeping") -> ToolCall:
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": category_key,
            "summary": "убрать номер 305",
            "room_number": "305",
        },
    )


async def test_unknown_category_key_escalates_and_creates_nothing(demo_tenant: uuid.UUID) -> None:
    # Модель выбрала category_key вне enum тенанта — на исполнении это ERR-AI-004,
    # оркестратор эскалирует к человеку, заявка не создаётся.
    bad_call = _housekeeping_call(category_key="spa")
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[bad_call]), MockTurn(tool_calls=[bad_call])]
    )

    with tenant_context(demo_tenant):
        first = await orchestrator.handle_message(message="нужно спа", provider=provider)
        assert first.kind is TurnKind.AWAITING_CONFIRMATION
        confirmed = await orchestrator.handle_message(
            message="да",
            pending_action=first.pending_action,
            provider=provider,
        )
        assert confirmed.kind is TurnKind.NEEDS_HUMAN
        assert confirmed.created_request_id is None
        assert await _request_total() == 0


async def test_unknown_tool_name_escalates(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[ToolCall(id="toolu_x", name="delete_everything", arguments={})])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="удали всё", provider=provider)
    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.created_request_id is None


async def test_fallback_confirmation_when_model_has_no_text(demo_tenant: uuid.UUID) -> None:
    # Модель вернула вызов инструмента без текста — оркестратор сам формулирует
    # подтверждающий вопрос из аргументов (резервный путь).
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_housekeeping_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="уберите 305", provider=provider)
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action == PendingAction(
        tool_name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "убрать номер 305",
            "room_number": "305",
        },
    )
    assert "305" in turn.reply_text  # вопрос содержит суть заявки

"""Тесты оркестратора на Fake-провайдере (Task 0015/0017.1, R-7).

Краевые случаи поверх golden-set v0 (test_golden_set_v0.py): эскалация при
нарушении контракта инструмента, резервный текст подтверждения и гейт P-9
(Task 0017.1, issue #31): ход подтверждения — структурная классификация
«confirm/decline/other» принудительным вызовом `resolve_confirmation`; на
`confirm` исполняется СОХРАНЁННЫЙ `pending_action`, без ре-эмиссии tool_use.
Полный путь «текст → LLM(mock) → инструмент → сервис requests → заявка» —
в golden-set.
"""

from __future__ import annotations

import uuid

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
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


def _confirmation_verdict(decision: str, reply: str = "") -> ToolCall:
    """Вердикт классификатора хода подтверждения — провайдер-протокол гейта P-9."""
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": decision, "reply": reply},
    )


async def test_confirmation_executes_stored_pending_action_without_reemission(
    demo_tenant: uuid.UUID,
) -> None:
    """Воспроизводящий тест issue #31 (красный до фикса Task 0017.1).

    На ходе подтверждения модель НЕ ре-эмитит `create_service_request` —
    возвращает только структурный вердикт `confirm`. Заявка обязана создаться
    из СОХРАНЁННОГО `pending_action`, а не из повторного tool_use модели.
    """
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("confirm", "Готово, передаю в службу.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
        assert await _request_total() == 0  # до подтверждения заявки нет (P-9)

        history = [
            LlmMessage(role="user", content="уберите номер 305"),
            LlmMessage(role="assistant", content=proposal.reply_text),
        ]
        done = await orchestrator.handle_message(
            message="да",
            history=history,
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert done.kind is TurnKind.ACTION_DONE
        assert done.created_request_id is not None
        assert done.reply_text == "Готово, передаю в службу."  # язык гостя — от модели
        assert await _request_total() == 1

    # Ход подтверждения — принудительная классификация: единственный служебный
    # инструмент, модель обязана его вызвать (tool_choice forced, не «авось повторит»).
    classification_request = provider.calls[1]
    assert classification_request.forced_tool == "resolve_confirmation"
    assert [tool.name for tool in classification_request.tools] == ["resolve_confirmation"]


async def test_declined_confirmation_creates_nothing_and_clears_gate(
    demo_tenant: uuid.UUID,
) -> None:
    """«Нет» гостя: вердикт `decline` → заявки нет, гейт гаснет (pending пуст)."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("decline", "Хорошо, не оформляю.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        declined = await orchestrator.handle_message(
            message="нет, не надо",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert declined.kind is TurnKind.REPLY
        assert declined.reply_text == "Хорошо, не оформляю."
        assert declined.pending_action is None  # канал очистит гейт в БД
        assert declined.created_request_id is None
        assert await _request_total() == 0


async def test_changed_mind_is_treated_as_new_request(demo_tenant: uuid.UUID) -> None:
    """Гость передумал (вердикт `other`) → старое предложение снято, сообщение
    обработано как новый запрос: новое предложение с новыми аргументами."""
    revised_call = ToolCall(
        id="toolu_2",
        name="create_service_request",
        arguments={
            "category_key": "engineering",
            "summary": "починить кран",
            "room_number": "305",
        },
    )
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("other")]),
            MockTurn(text="Починить кран в номере 305 — оформить?", tool_calls=[revised_call]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        revised = await orchestrator.handle_message(
            message="лучше почините кран",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert revised.kind is TurnKind.AWAITING_CONFIRMATION
        assert revised.pending_action == PendingAction(
            tool_name="create_service_request", arguments=revised_call.arguments
        )
        assert await _request_total() == 0  # ничего не исполнено без нового «да»


async def test_classifier_protocol_violation_falls_back_to_new_request(
    demo_tenant: uuid.UUID,
) -> None:
    """Фейк нарушил протокол классификации (текст вместо вердикта — боевой
    forced tool choice так ответить не может) → безопасный fallback `other`:
    ничего не исполняется молча, сообщение уходит обычным путём."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(text="Да, конечно!"),  # нарушение: нет вызова resolve_confirmation
            MockTurn(text="Уточните, пожалуйста, что оформить."),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        turn = await orchestrator.handle_message(
            message="да",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert turn.kind is TurnKind.REPLY
        assert turn.created_request_id is None
        assert await _request_total() == 0


async def test_unknown_category_key_escalates_and_creates_nothing(demo_tenant: uuid.UUID) -> None:
    # Модель выбрала category_key вне enum тенанта — на исполнении это ERR-AI-004,
    # оркестратор эскалирует к человеку, заявка не создаётся.
    bad_call = _housekeeping_call(category_key="spa")
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[bad_call]), MockTurn(tool_calls=[_confirmation_verdict("confirm")])]
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

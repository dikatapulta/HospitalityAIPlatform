"""Несколько просьб в одном сообщении гостя (spec 0078, issue #90) — Fake-провайдер.

Каждое действие хода — своё: вопросы идут гостю одной репликой, одно «да»
исполняет все (пакет), «нет» — ни одного, остальное — новый запрос. Одиночное
действие проверяют прежние тесты `test_orchestrator.py` без правок.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest
from structlog.testing import capture_logs

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.evals import multi_need
from hospitality.ai.evals.bakeoff import _ensure_eval_tenant
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import PreparedCall, ToolTurnContext
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

CLEANING_QUESTION = "Кофе через меня заказать не получится — поможет ресепшен. Оформить уборку?"
TOWELS_QUESTION = "Принести полотенца в номер 305?"
GUEST_MESSAGE = "убери номер, принеси полотенца и 2 пачки кофе"


def _call(summary: str, question: str, *, category_key: str = "housekeeping") -> ToolCall:
    return ToolCall(
        id=f"toolu_{summary}",
        name="create_service_request",
        arguments={
            "category_key": category_key,
            "summary": summary,
            "room_number": "305",
            "confirmation_question": question,
            "guest_language": "ru",
        },
    )


def _cleaning() -> ToolCall:
    return _call("убрать номер", CLEANING_QUESTION)


def _towels() -> ToolCall:
    return _call("полотенца", TOWELS_QUESTION)


def _verdict(decision: str, reply: str = "") -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": decision, "reply": reply},
    )


async def _summaries() -> list[str]:
    page = await requests_api.list_requests(limit=10, offset=0)
    return sorted(request.summary for request in page.items)


async def _propose(provider: ScriptedLlmProvider) -> orchestrator.OrchestratorTurn:
    return await orchestrator.handle_message(message=GUEST_MESSAGE, provider=provider)


async def _answer(
    provider: ScriptedLlmProvider, proposal: orchestrator.OrchestratorTurn, message: str
) -> orchestrator.OrchestratorTurn:
    history = [
        LlmMessage(role="user", content=GUEST_MESSAGE),
        LlmMessage(role="assistant", content=proposal.reply_text),
    ]
    return await orchestrator.handle_message(
        message=message,
        history=history,
        pending_action=proposal.pending_action,
        provider=provider,
    )


async def test_two_requests_wait_as_one_batch(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_cleaning(), _towels()])])
    with tenant_context(demo_tenant), capture_logs() as logs:
        turn = await _propose(provider)
        assert await _summaries() == []  # до «да» заявок нет (P-9)
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.reply_text == f"{CLEANING_QUESTION}\n{TOWELS_QUESTION}"
    assert turn.pending_action is not None
    assert [call.arguments["summary"] for call in turn.pending_action.calls()] == [
        "убрать номер",
        "полотенца",
    ]
    assert turn.created_request_ids == ()
    proposed = next(log for log in logs if log["event"] == "actions_proposed")
    assert proposed["count"] == 2


async def test_yes_creates_every_request_of_the_batch(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cleaning(), _towels()]),
            MockTurn(tool_calls=[_verdict("confirm", "Готово, передал уборку и полотенца.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await _propose(provider)
        done = await _answer(provider, proposal, "да")
        assert await _summaries() == ["полотенца", "убрать номер"]
    assert done.kind is TurnKind.ACTION_DONE
    assert len(done.created_request_ids) == 2
    assert done.created_request_id == done.created_request_ids[0]
    assert done.reply_text == "Готово, передал уборку и полотенца."
    # Классификатор решал за пакет целиком и видел оба действия.
    system = provider.calls[1].system or ""
    assert "# Pending actions awaiting the guest's confirmation" in system
    assert "agrees to all of" in system
    assert "убрать номер" in system and "полотенца" in system


async def test_empty_classifier_reply_falls_back_to_one_done_text(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[_cleaning(), _towels()]), MockTurn(tool_calls=[_verdict("confirm")])]
    )
    with tenant_context(demo_tenant):
        done = await _answer(provider, await _propose(provider), "да")
    # Резерв обоих — один и тот же `DONE_TEXT`: гостю он один раз.
    assert done.reply_text == registry.done_text("create_service_request", {})


async def test_no_creates_nothing(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[_cleaning(), _towels()]), MockTurn(tool_calls=[_verdict("decline")])]
    )
    with tenant_context(demo_tenant):
        declined = await _answer(provider, await _propose(provider), "нет, не надо")
        assert await _summaries() == []
    assert declined.kind is TurnKind.REPLY
    assert declined.pending_action is None


async def test_agreeing_to_some_is_a_new_request(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cleaning(), _towels()]),
            MockTurn(tool_calls=[_verdict("other")]),
            MockTurn(tool_calls=[_towels()]),
        ]
    )
    with tenant_context(demo_tenant):
        again = await _answer(provider, await _propose(provider), "только полотенца")
        assert await _summaries() == []
    assert again.kind is TurnKind.AWAITING_CONFIRMATION
    assert again.reply_text == TOWELS_QUESTION
    assert again.pending_action is not None
    assert again.pending_action.more == ()


async def test_repeated_call_is_one_action(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_towels(), _towels()])])
    with tenant_context(demo_tenant):
        turn = await _propose(provider)
    assert turn.reply_text == TOWELS_QUESTION
    assert turn.pending_action is not None
    assert turn.pending_action.more == ()


async def test_urgent_is_created_now_and_the_rest_waits(demo_tenant: uuid.UUID) -> None:
    leak = _call("течёт вода с потолка", "Оформить инженерам?", category_key="engineering")
    leak.arguments["is_urgent"] = True
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[leak, _towels()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="заливает номер с потолка, и полотенца принесите", provider=provider
        )
        assert await _summaries() == ["течёт вода с потолка"]
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert len(turn.created_request_ids) == 1
    assert turn.pending_action is not None
    assert turn.pending_action.calls()[0].arguments["summary"] == "полотенца"
    assert turn.reply_text.endswith(TOWELS_QUESTION)
    assert "Оформить инженерам?" not in turn.reply_text  # вопрос о созданной — ложь


async def test_refusal_of_one_keeps_the_others(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_prepare = registry.prepare

    async def prepare(
        tool_name: str, arguments: dict[str, Any], context: ToolTurnContext
    ) -> PreparedCall:
        if arguments.get("summary") == "вода":
            return PreparedCall(refusal="Вода на сегодня закончилась.")
        return await real_prepare(tool_name, arguments, context)

    monkeypatch.setattr(registry, "prepare", prepare)
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[_call("вода", "Принести воду?"), _towels()])]
    )
    with tenant_context(demo_tenant):
        turn = await _propose(provider)
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.reply_text == f"Вода на сегодня закончилась.\n\n{TOWELS_QUESTION}"
    assert turn.pending_action is not None
    assert len(turn.pending_action.calls()) == 1


async def test_failure_of_second_escalates_and_keeps_the_first(demo_tenant: uuid.UUID) -> None:
    broken = _call("массаж", "Записать на массаж?", category_key="spa")  # вне enum тенанта
    pending = PendingAction(
        tool_name="create_service_request",
        arguments=_towels().arguments,
        more=(PendingAction(tool_name="create_service_request", arguments=broken.arguments),),
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_verdict("confirm", "Готово.")])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="да", pending_action=pending, provider=provider
        )
        assert await _summaries() == ["полотенца"]
    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert len(turn.created_request_ids) == 1  # канал привяжет её к диалогу
    assert turn.escalation is not None
    assert turn.escalation.reason is EscalationReason.TOOL_EXECUTION_FAILED
    assert turn.escalation.action_summary == "массаж"


async def test_staff_call_wins_and_leaves_a_trace(demo_tenant: uuid.UUID) -> None:
    staff = ToolCall(
        id="toolu_staff",
        name="escalate_to_staff",
        arguments={"reply_to_guest": "Зову сотрудника.", "reason": "гость просит менеджера"},
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_towels(), staff, _cleaning()])])
    with tenant_context(demo_tenant), capture_logs() as logs:
        turn = await _propose(provider)
        assert await _summaries() == []
    assert turn.kind is TurnKind.NEEDS_HUMAN
    superseded = next(log for log in logs if log["event"] == "actions_superseded_by_escalation")
    assert superseded["tools"] == ["create_service_request", "create_service_request"]


async def test_evals_plumbing_on_fake_provider(
    canonical_database: None, service_requests_enabled: None
) -> None:
    """Обвязка платного набора проверяется бесплатно: eval-тенант, пакет и «да» на него."""
    await _ensure_eval_tenant()
    yes = multi_need.Scenario("ru", "yes")
    fix = _call("кондиционер", "Починить кондиционер?", category_key="engineering")
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cleaning(), fix]),
            MockTurn(tool_calls=[_verdict("confirm", "Готово.")]),
        ]
    )
    ok, detail = await multi_need._run_one(provider, yes)
    assert ok, detail
    # «Да» ушло классификатору с историей хода предложения.
    assert provider.calls[1].messages[-2].content == f"{CLEANING_QUESTION}\nПочинить кондиционер?"
    assert len(multi_need.SCENARIOS) == 9


def test_evals_check_catches_merged_lost_and_wrong_language() -> None:
    """Офлайн-проверка судьи платного набора `evals/multi_need` (spec 0078 §7)."""

    def awaiting(reply: str, *categories: str) -> orchestrator.OrchestratorTurn:
        first, *rest = (
            PendingAction(tool_name="create_service_request", arguments={"category_key": key})
            for key in categories
        )
        return orchestrator.OrchestratorTurn(
            TurnKind.AWAITING_CONFIRMATION, reply, pending_action=replace(first, more=tuple(rest))
        )

    two, three = multi_need.Scenario("ru", "two"), multi_need.Scenario("en", "three")
    assert multi_need._check(two, awaiting("Убрать? Починить?", "housekeeping", "engineering"))[0]
    assert not multi_need._check(two, awaiting("Убрать?", "housekeeping"))[0]  # слиты
    assert not multi_need._check(two, awaiting("?", "housekeeping", "housekeeping"))[0]
    taxi = "No taxi here — reception will help. Clean? Towels?"
    lost = "Clean? Towels?"
    assert multi_need._check(three, awaiting(taxi, "housekeeping", "housekeeping"))[0]
    assert not multi_need._check(three, awaiting(lost, "housekeeping", "housekeeping"))[0]
    assert not multi_need._check(three, awaiting(f"{taxi} Такси", "housekeeping", "it"))[0]
    yes = multi_need.Scenario("kk", "yes")
    ids = (uuid.uuid4(), uuid.uuid4())
    done = orchestrator.OrchestratorTurn(TurnKind.ACTION_DONE, "Дайын.", created_request_ids=ids)
    assert multi_need._check(yes, done)[0]
    assert not multi_need._check(yes, replace(done, created_request_ids=ids[:1]))[0]

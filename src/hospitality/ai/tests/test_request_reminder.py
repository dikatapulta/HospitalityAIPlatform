"""Напоминание гостя о заявке в оркестраторе (spec 0070 §4, §8, issue #834).

Проверяется то, чего не видно ни из схемы, ни из промпта: напоминание —
сопутствующий вызов, как сигнал «факта нет», и у действия того же хода не
выигрывает ни в какой позиции («где полотенца? и принесите воды» не теряет
заявку на воду); счёт ложится на заявку на любом исходе хода, эскалацию
включая; id вне снапшота и заявка, закрытая между ходами, — пропуск без
эскалации; реплика доезжает до гостя первым абзацем, с резервом.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.evals import request_reminder as evals
from hospitality.ai.evals.bakeoff import _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import TurnKind
from hospitality.ai.tools import remind_about_request
from hospitality.ai.tools.base import ActiveRequest
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

REMINDER_REPLY = "Отметил ваше напоминание в заявке #1 — полотенца. Сотрудники о ней знают."
CONFIRMATION = "Оформить заявку на воду в номер 305?"


async def _create_request(summary: str = "полотенца в 305") -> requests_api.ServiceRequestRead:
    categories = await requests_api.list_categories()
    category_id = next(c.id for c in categories if c.key == "housekeeping")
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary=summary,
        )
    )


def _as_active(request: requests_api.ServiceRequestRead) -> ActiveRequest:
    return ActiveRequest(
        id=request.id,
        status=request.status,
        summary=request.summary,
        daily_number=request.daily_number,
    )


def _remind_call(request_id: uuid.UUID, reply: str | None = REMINDER_REPLY) -> ToolCall:
    arguments: dict[str, object] = {"request_id": str(request_id)}
    if reply is not None:
        arguments["reply_to_guest"] = reply
    return ToolCall(id="toolu_remind", name=remind_about_request.NAME, arguments=arguments)


def _water_call() -> ToolCall:
    """Новая просьба того же хода: заявка класса CONFIRM_GUEST (гейт P-9 стоит)."""
    return ToolCall(
        id="toolu_water",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "принести воду",
            "room_number": "305",
            "confirmation_question": CONFIRMATION,
            "guest_language": "ru",
        },
    )


async def _reminder_count(request_id: uuid.UUID) -> int:
    return (await requests_api.get_request(request_id)).guest_reminder_count


async def test_reminder_alone_replies_from_its_argument_and_counts(
    demo_tenant: uuid.UUID,
) -> None:
    """Напоминание единственным вызовом: ход REPLY, реплика — из аргумента, а не
    из свободного текста (он здесь не пуст намеренно), счёт и время — на заявке.

    Режим по умолчанию — консультации: флаг заявок напоминание не выключает.
    """
    with tenant_context(demo_tenant):
        request = await _create_request()
        provider = ScriptedLlmProvider(
            [MockTurn(text="Сейчас уточню.", tool_calls=[_remind_call(request.id)])]
        )
        turn = await orchestrator.handle_message(
            message="где мои полотенца?", active_requests=[_as_active(request)], provider=provider
        )
        stored = await requests_api.get_request(request.id)

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == REMINDER_REPLY
    assert turn.escalation is None
    assert stored.guest_reminder_count == 1
    assert stored.guest_reminded_at is not None
    declared = [tool.name for tool in provider.calls[0].tools]
    assert remind_about_request.NAME in declared


async def test_reminder_schema_keeps_the_rules_of_the_reply(demo_tenant: uuid.UUID) -> None:
    """Реплику модель пишет до решения кода, кого звать (spec 0070 §10): «сообщил
    менеджеру» и обещание срока были бы неправдой гостю. Тест стережёт текст
    схемы от тихого удаления; модель он не спрашивает."""
    with tenant_context(demo_tenant):
        request = await _create_request()
    spec = remind_about_request.build_spec((_as_active(request),))
    reply_rule = spec.input_schema["properties"]["reply_to_guest"]["description"]
    assert "Promise no time" in reply_rule
    assert "never say that a manager or anyone else was alerted" in reply_rule
    assert "language of the guest's last message" in reply_rule
    assert spec.input_schema["properties"]["request_id"]["enum"] == [str(request.id)]


@pytest.mark.parametrize("reminder_first", [True, False])
async def test_reminder_never_wins_over_the_action_of_the_turn(
    demo_tenant: uuid.UUID, service_requests_enabled: None, reminder_first: bool
) -> None:
    """«Где полотенца? и принесите воды»: заявка на воду идёт в гейт P-9 при любой
    позиции напоминания, реплика напоминания — первым абзацем перед вопросом."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        calls = [_remind_call(request.id), _water_call()]
        provider = ScriptedLlmProvider(
            [MockTurn(tool_calls=calls if reminder_first else calls[::-1])]
        )
        turn = await orchestrator.handle_message(
            message="где полотенца? и принесите воды",
            active_requests=[_as_active(request)],
            provider=provider,
        )
        count = await _reminder_count(request.id)

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action is not None
    assert turn.pending_action.tool_name == "create_service_request"
    assert turn.reply_text == f"{REMINDER_REPLY}\n\n{CONFIRMATION}"
    assert count == 1


async def test_escalation_of_the_turn_speaks_alone_but_the_reminder_is_recorded(
    demo_tenant: uuid.UUID,
) -> None:
    """«Позовите менеджера, я второй раз прошу!» — `escalate_to_staff`: гостю только
    её текст (человек уже идёт), напоминание всё равно на заявке."""
    staff_reply = "Передал сотрудникам — к вам подойдут."
    with tenant_context(demo_tenant):
        request = await _create_request()
        staff_call = ToolCall(
            id="toolu_staff",
            name="escalate_to_staff",
            arguments={"summary": "гость просит менеджера", "reply_to_guest": staff_reply},
        )
        provider = ScriptedLlmProvider(
            [MockTurn(tool_calls=[_remind_call(request.id), staff_call])]
        )
        turn = await orchestrator.handle_message(
            message="позовите менеджера, я второй раз прошу!",
            active_requests=[_as_active(request)],
            provider=provider,
        )
        count = await _reminder_count(request.id)

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.escalation is not None
    assert turn.escalation.reason is EscalationReason.GUEST_REQUESTED_STAFF
    assert turn.reply_text == staff_reply
    assert count == 1


async def test_failed_action_escalates_with_its_own_text_and_keeps_the_reminder(
    demo_tenant: uuid.UUID,
) -> None:
    """Сбой хода (неизвестный инструмент) — эскалация её текстом; напоминание,
    записанное до выбора действия, не откатывается."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        broken = ToolCall(id="toolu_x", name="delete_everything", arguments={})
        provider = ScriptedLlmProvider([MockTurn(tool_calls=[broken, _remind_call(request.id)])])
        turn = await orchestrator.handle_message(
            message="где полотенца?", active_requests=[_as_active(request)], provider=provider
        )
        count = await _reminder_count(request.id)

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.escalation is not None
    assert turn.escalation.reason is EscalationReason.UNKNOWN_TOOL
    assert turn.reply_text == orchestrator._ESCALATION_TEXT
    assert count == 1


async def test_request_outside_the_snapshot_is_skipped_without_escalation(
    demo_tenant: uuid.UUID,
) -> None:
    """Заявка того же отеля, но не этого диалога: счёт не трогается, ход — REPLY.
    Гость спрашивал о статусе, а не просил действия: звать человека не за что."""
    with tenant_context(demo_tenant):
        own = await _create_request()
        foreign = await _create_request("чужая заявка")
        provider = ScriptedLlmProvider([MockTurn(tool_calls=[_remind_call(foreign.id)])])
        turn = await orchestrator.handle_message(
            message="где полотенца?", active_requests=[_as_active(own)], provider=provider
        )
        counts = (await _reminder_count(own.id), await _reminder_count(foreign.id))

    assert turn.kind is TurnKind.REPLY
    assert turn.escalation is None
    assert counts == (0, 0)


async def test_request_closed_between_turns_is_skipped(demo_tenant: uuid.UUID) -> None:
    """Заявку закрыли после снапшота хода: домен отвечает None, счёта нет, ход
    не падает и в эскалацию не уходит."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        snapshot = [_as_active(request)]
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
        )
        provider = ScriptedLlmProvider([MockTurn(tool_calls=[_remind_call(request.id)])])
        turn = await orchestrator.handle_message(
            message="где полотенца?", active_requests=snapshot, provider=provider
        )
        count = await _reminder_count(request.id)

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == REMINDER_REPLY
    assert count == 0


async def test_each_request_counts_once_per_turn(demo_tenant: uuid.UUID) -> None:
    """Повтор одного id за ход — одна запись; две заявки — по записи на каждую,
    реплики — абзацами по порядку, одинаковые не повторяются."""
    water_reply = "Отметил напоминание в заявке #2 — вода."
    with tenant_context(demo_tenant):
        towels = await _create_request()
        water = await _create_request("вода в 305")
        provider = ScriptedLlmProvider(
            [
                MockTurn(
                    tool_calls=[
                        _remind_call(towels.id),
                        _remind_call(towels.id),
                        _remind_call(water.id, water_reply),
                    ]
                )
            ]
        )
        turn = await orchestrator.handle_message(
            message="где полотенца? где вода?",
            active_requests=[_as_active(towels), _as_active(water)],
            provider=provider,
        )
        counts = (await _reminder_count(towels.id), await _reminder_count(water.id))

    assert counts == (1, 1)
    assert turn.reply_text == f"{REMINDER_REPLY}\n\n{water_reply}"


async def test_reply_falls_back_to_model_text_then_to_the_trilingual_stub(
    demo_tenant: uuid.UUID,
) -> None:
    """Приоритет реплики: аргумент → свободный текст модели → резерв kk/ru/en (§7)."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        snapshot = [_as_active(request)]
        provider = ScriptedLlmProvider(
            [
                MockTurn(text="Заявка в работе.", tool_calls=[_remind_call(request.id, None)]),
                MockTurn(tool_calls=[_remind_call(request.id, "  ")]),
            ]
        )
        from_model_text = await orchestrator.handle_message(
            message="где полотенца?", active_requests=snapshot, provider=provider
        )
        from_stub = await orchestrator.handle_message(
            message="где полотенца?", active_requests=snapshot, provider=provider
        )
        count = await _reminder_count(request.id)

    assert from_model_text.reply_text == "Заявка в работе."
    assert from_stub.reply_text == remind_about_request.FALLBACK_REPLY
    assert count == 2


async def test_reminder_and_unanswered_signal_both_reach_the_guest(
    demo_tenant: uuid.UUID,
) -> None:
    """«Где полотенца и есть ли утюг?»: два сопутствующих вызова, ни один не теряет
    реплику — сначала напоминание, затем ответ «факта нет»; вопрос — в справочник."""
    signal_reply = "Про утюг в моей справке нет — на ресепшене подскажут."
    signal = ToolCall(
        id="toolu_signal",
        name=orchestrator.UNANSWERED_QUESTION_TOOL_NAME,
        arguments={"question": "есть ли утюг", "reply_to_guest": signal_reply},
    )
    with tenant_context(demo_tenant):
        request = await _create_request()
        provider = ScriptedLlmProvider([MockTurn(tool_calls=[signal, _remind_call(request.id)])])
        turn = await orchestrator.handle_message(
            message="где полотенца и есть ли утюг?",
            active_requests=[_as_active(request)],
            provider=provider,
        )

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == f"{REMINDER_REPLY}\n\n{signal_reply}"
    assert turn.unanswered_question == "есть ли утюг"


async def test_evals_plumbing_on_fake_provider(
    canonical_database: None, service_requests_enabled: None
) -> None:
    """Обвязка платных evals проверяется бесплатно: eval-тенант, свежая заявка на
    сценарий, счёт с заявки, уборка за собой и оценка реплики."""
    await _ensure_eval_tenant()
    thanks = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("en", "thanks"))
    remind = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("ru", "remind"))
    provider = ScriptedLlmProvider(
        [MockTurn(text="You're welcome!"), MockTurn(text="Ваша заявка в работе.")]
    )

    assert (await evals._run_one(provider, thanks))[0] is True
    ok, detail = await evals._run_one(provider, remind)
    assert ok is False
    assert detail.startswith("напоминаний 0, ждали 1")
    with tenant_context(await _eval_tenant_id()):
        page = await requests_api.list_requests(limit=10, offset=0)
    assert {request.status for request in page.items} == {requests_api.RequestStatus.CANCELLED}
    assert len(evals.SCENARIOS) == 21

    promised = orchestrator.OrchestratorTurn(
        kind=TurnKind.REPLY, reply_text="Принесут через 10 минут."
    )
    alerted = orchestrator.OrchestratorTurn(
        kind=TurnKind.REPLY, reply_text="Я сообщил менеджеру о заявке #1."
    )
    assert evals._check(remind, promised, 1)[0] is False
    assert evals._check(remind, alerted, 1)[0] is False
    assert evals._check(remind, orchestrator.OrchestratorTurn(TurnKind.REPLY, REMINDER_REPLY), 1)[0]

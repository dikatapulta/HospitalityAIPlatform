"""Инструмент `escalate_to_staff` в оркестраторе (spec 0040, issue #101).

Предмет — обещание человека без эффекта: до инструмента «уточню у коллег»
приходило `REPLY`, и эскалации не было. Здесь — что вызов модели становится
`NEEDS_HUMAN` с контекстом эскалации, откуда берётся реплика гостю, что вызов
выигрывает у соседнего действия и что инструмент живёт ровно под флагом заявок.
Путь «ход гостя → карточка в кабинете» — в channels/common/tests.
"""

from __future__ import annotations

import uuid

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import TurnKind
from hospitality.ai.tools import escalate_to_staff
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


def _staff_call(**arguments: object) -> ToolCall:
    return ToolCall(id="toolu_staff", name="escalate_to_staff", arguments=dict(arguments))


async def test_staff_call_becomes_escalation_with_reply_from_argument(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """DoD #101: вызов инструмента — исход `NEEDS_HUMAN`, причина своя, суть
    просьбы и комната из привязки уезжают персоналу, гость читает аргумент."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                text="свободный текст",
                tool_calls=[
                    _staff_call(
                        summary="Гость хочет поговорить с менеджером о счёте",
                        reply_to_guest="Передал вашу просьбу — к вам подойдут.",
                    )
                ],
            )
        ]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="позовите менеджера", verified_room_number="305", provider=provider
        )

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.reply_text == "Передал вашу просьбу — к вам подойдут."
    assert turn.pending_action is None
    assert turn.escalation is not None
    assert turn.escalation.reason is EscalationReason.GUEST_REQUESTED_STAFF
    assert turn.escalation.error_code == escalate_to_staff.ERR_AI_STAFF_REQUESTED
    assert turn.escalation.action_summary == "Гость хочет поговорить с менеджером о счёте"
    assert turn.escalation.room_number == "305"


async def test_reply_falls_back_to_model_text_then_to_static_text(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Аргумента нет — свободный текст модели; нет и его — двуязычный резерв.
    Кривой вызов человека всё равно зовёт: гость уже его ждёт."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Зову сотрудника.", tool_calls=[_staff_call()]),
            MockTurn(tool_calls=[_staff_call(summary=42, reply_to_guest="   ")]),
        ]
    )
    with tenant_context(demo_tenant):
        with_text = await orchestrator.handle_message(message="человека!", provider=provider)
        bare = await orchestrator.handle_message(message="человека!", provider=provider)

    assert with_text.reply_text == "Зову сотрудника."
    assert bare.reply_text == escalate_to_staff.FALLBACK_REPLY
    assert bare.kind is TurnKind.NEEDS_HUMAN
    assert bare.escalation is not None
    assert bare.escalation.action_summary is None


async def test_staff_call_wins_over_a_request_on_the_same_turn(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Два вызова за ход: эскалация выигрывает при любой позиции, заявка не
    черновится и не создаётся. Потерянная эскалация — обещание без эффекта,
    просьбу же человек прочтёт в дословной реплике карточки."""
    request_call = ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={"category_key": "housekeeping", "summary": "полотенца", "guest_language": "ru"},
    )
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[request_call, _staff_call(summary="s", reply_to_guest="Передал.")])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="полотенца и позовите менеджера", provider=provider
        )
        total = (await requests_api.list_requests(limit=1, offset=0)).total

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.pending_action is None
    assert total == 0


async def test_consultation_mode_has_no_staff_tool_and_forbids_the_promise(
    demo_tenant: uuid.UUID,
) -> None:
    """Флаг заявок выключен (умолчание, гостевая копия этапов 1–2): позвать
    некого, поэтому инструмента нет, а промпт ведёт гостя на ресепшен вместо
    предложения сотрудника — «да» на такое предложение никуда бы не ушло."""
    provider = ScriptedLlmProvider([MockTurn(text="Здравствуйте!")])
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="позовите менеджера", provider=provider)

    llm_request = provider.calls[0]
    system = llm_request.system or ""
    # Сотрудника позвать нечем; остаётся только служебный сигнал «факта нет»
    # (spec 0036 §6) — заявок он не создаёт и человека не зовёт.
    assert [tool.name for tool in llm_request.tools] == [orchestrator.UNANSWERED_QUESTION_TOOL_NAME]
    assert "You have NO way to call a staff member" in system
    assert "point the guest to the reception desk instead" in system


async def test_requests_mode_sends_the_staff_tool_and_its_prompt_section(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    provider = ScriptedLlmProvider([MockTurn(text="Здравствуйте!")])
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="привет", provider=provider)

    llm_request = provider.calls[0]
    system = llm_request.system or ""
    assert "escalate_to_staff" in [tool.name for tool in llm_request.tools]
    assert "# Calling a staff member" in system
    assert "You have NO way to call a staff member" not in system
    # Обещание «проверю у сотрудника» v5 — само по себе обещание без эффекта.
    assert "you will check with a staff member" not in system


def test_spec_describes_guest_text_in_english_and_requires_both_fields() -> None:
    """Язык аргумента тянется за языком описания (#342): гостевой текст
    описан по-английски, кириллицы в схеме нет."""
    spec = escalate_to_staff.build_spec()
    assert spec.input_schema["required"] == ["summary", "reply_to_guest"]
    schema_text = spec.description + str(spec.input_schema)
    assert not any("а" <= char.lower() <= "я" for char in schema_text)

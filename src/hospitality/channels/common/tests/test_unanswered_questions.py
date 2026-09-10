"""Строка «вопрос без ответа» на стороне канала (spec 0036 §6, issue #334).

Инварианты таблицы `unanswered_questions`: один ход с сигналом — ровно одна
строка; ретеншн гостевых текстов (spec 0032) сносит старые строки и не трогает
свежие; соседний тенант своих вопросов в чужом справочнике не видит (P-4); а на
ходу, кончившемся эскалацией, обещание «подключу сотрудника» сильнее строки
справочника — она пишется ПОСЛЕ публикации эскалации.

Проверяется через `run_guest_turn` — тот самый путь, которым ходит живой гость:
поле исхода оркестратора существует ради этой строки, и проверять их порознь
значило бы не проверить связь.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import UNANSWERED_QUESTION_TOOL_NAME
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common import guest_turn as guest_turn_module
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.models import ConversationEscalation, UnansweredQuestion
from hospitality.channels.common.retention import enforce_guest_text_retention
from hospitality.channels.common.store import ensure_conversation, insert_inbound_message
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

RETENTION_DAYS = 90
SIGNAL_REPLY = "Про утюг в моей справке нет — на ресепшене подскажут точно."


def _signal_turn(question: str = "есть ли в номере утюг") -> MockTurn:
    return MockTurn(
        tool_calls=[
            ToolCall(
                id="toolu_signal",
                name=UNANSWERED_QUESTION_TOOL_NAME,
                arguments={"question": question, "reply_to_guest": SIGNAL_REPLY},
            )
        ]
    )


async def _guest_turn(
    tenant_id: uuid.UUID, *, external_id: str = "4242", question: str = "есть ли в номере утюг"
) -> list[str]:
    """Один ход живого гостя, кончившийся сигналом; вернуть отправленное гостю."""
    sent: list[str] = []

    async def reply(text: str) -> None:
        sent.append(text)

    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", external_id)
        message_id = await insert_inbound_message(
            conversation_id,
            NormalizedMessage(
                channel="telegram",
                chat_id=external_id,
                kind=MessageKind.TEXT,
                text="есть ли утюг?",
                idempotency_key=f"telegram:update:{uuid.uuid4().hex}",
                external_message_id="1",
            ),
            correlation_id="test",
        )
        assert message_id is not None
        await run_guest_turn(
            conversation_id,
            "есть ли утюг?",
            message_id,
            external_id=external_id,
            rate_limit_key=external_id,
            reply=reply,
            provider=ScriptedLlmProvider([_signal_turn(question)]),
        )
    return sent


async def _rows(tenant_id: uuid.UUID) -> list[UnansweredQuestion]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list((await session.scalars(select(UnansweredQuestion))).all())


async def test_turn_with_the_signal_writes_exactly_one_row(demo_tenant: uuid.UUID) -> None:
    """Поле исхода `unanswered_question` → ровно одна строка, и гость получает
    реплику сигнала: без строки справочник не пополнится, без реплики гость
    останется без ответа."""
    sent = await _guest_turn(demo_tenant)

    rows = await _rows(demo_tenant)
    assert len(rows) == 1
    assert rows[0].question == "есть ли в номере утюг"
    assert sent == [SIGNAL_REPLY]


async def test_turn_without_the_signal_writes_nothing(demo_tenant: uuid.UUID) -> None:
    """Обычный ход строк не плодит — защита от «канал пишет на каждый ход»."""
    sent: list[str] = []

    async def reply(text: str) -> None:
        sent.append(text)

    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "4242")
        message_id = await insert_inbound_message(
            conversation_id,
            NormalizedMessage(
                channel="telegram",
                chat_id="4242",
                kind=MessageKind.TEXT,
                text="спасибо",
                idempotency_key="telegram:update:1",
                external_message_id="1",
            ),
            correlation_id="test",
        )
        assert message_id is not None
        await run_guest_turn(
            conversation_id,
            "спасибо",
            message_id,
            external_id="4242",
            rate_limit_key="4242",
            reply=reply,
            provider=ScriptedLlmProvider([MockTurn(text="Пожалуйста!")]),
        )

    assert await _rows(demo_tenant) == []
    assert sent == ["Пожалуйста!"]


async def test_retention_deletes_old_questions_and_keeps_fresh_ones(
    demo_tenant: uuid.UUID,
) -> None:
    """spec 0032: `question` — текст гостя, и живёт он 90 дней.

    Возраст считается по САМОЙ строке, а не по диалогу: диалог гостя, который
    пишет каждую неделю, каскада не даст никогда, а обещание политики дано на
    текст. Свежая строка того же диалога обязана остаться.
    """
    await _guest_turn(demo_tenant, question="старый вопрос")
    with tenant_context(demo_tenant):
        async with session_scope() as session:
            await session.execute(
                update(UnansweredQuestion).values(
                    created_at=utc_now() - timedelta(days=RETENTION_DAYS + 1)
                )
            )
    await _guest_turn(demo_tenant, question="свежий вопрос")

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    rows = await _rows(demo_tenant)
    assert [row.question for row in rows] == ["свежий вопрос"]
    assert stats.unanswered_questions_deleted == 1


async def test_two_tenants_do_not_see_each_others_questions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4/ADR-003: таблица тенантная, RLS-блок скопирован с канона 0002.

    Справочник — данные отеля, и вопросы его гостей на странице соседнего отеля
    не появляются ни строкой.
    """
    tenant_a, tenant_b = two_tenants
    await _guest_turn(tenant_a, external_id="111", question="вопрос отеля A")
    await _guest_turn(tenant_b, external_id="222", question="вопрос отеля B")

    assert [row.question for row in await _rows(tenant_a)] == ["вопрос отеля A"]
    assert [row.question for row in await _rows(tenant_b)] == ["вопрос отеля B"]


async def test_escalation_survives_a_broken_question_write(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок записей на ходу с эскалацией: сначала эскалация, потом строка.

    На таком ходу (spec 0036 §6) пишется и то и другое, а упасть может любая
    запись — порядок решает, что уцелеет. Обещание «подключу сотрудника» обязано
    быть правдой (spec 0022), строка справочника такого обещания не несёт.
    Тест ломает запись строки и требует, чтобы эскалация уже была в БД: при
    обратном порядке она бы не публиковалась вовсе.
    """

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(guest_turn_module, "record_unanswered_question", broken)

    sent: list[str] = []

    async def reply(text: str) -> None:
        sent.append(text)

    # Сигнал + инструмент, которого нет в реестре: ход кончается NEEDS_HUMAN.
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="toolu_signal",
                        name=UNANSWERED_QUESTION_TOOL_NAME,
                        arguments={"question": "есть ли утюг", "reply_to_guest": SIGNAL_REPLY},
                    ),
                    ToolCall(id="toolu_x", name="order_taxi", arguments={"to": "аэропорт"}),
                ]
            )
        ]
    )

    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "4242")
        message_id = await insert_inbound_message(
            conversation_id,
            NormalizedMessage(
                channel="telegram",
                chat_id="4242",
                kind=MessageKind.TEXT,
                text="есть ли утюг и вызовите такси",
                idempotency_key="telegram:update:7",
                external_message_id="7",
            ),
            correlation_id="test",
        )
        assert message_id is not None
        with pytest.raises(RuntimeError):
            await run_guest_turn(
                conversation_id,
                "есть ли утюг и вызовите такси",
                message_id,
                external_id="4242",
                rate_limit_key="4242",
                reply=reply,
                provider=provider,
            )
        async with session_scope() as session:
            escalations = await session.scalar(
                select(func.count()).select_from(ConversationEscalation)
            )

    assert escalations == 1  # эскалация закоммичена ДО падения записи строки
    assert await _rows(demo_tenant) == []
    assert sent == []  # реплики не было: ход оборвался, но обещания и не прозвучало

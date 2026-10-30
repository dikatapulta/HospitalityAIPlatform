"""Страховка хода гостя (issue #43): сбой до реплики — деградация, а не тишина.

Ключ идемпотентности отмечает «сообщение получено», а не «ход завершён»:
упавший ход повтор доставки уже не перезапустит. Ход зовётся напрямую,
сбой подставляется в шаг хода — как в `test_escalation_without_staff.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from hospitality.ai import orchestrator
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.channels.common import guest_turn
from hospitality.channels.common.guest_turn import (
    DEGRADED_REPLY,
    ERR_GUEST_TURN_FAILED,
    no_staff_reply,
    run_guest_turn,
)
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records

PHONE = "+7 727 000 00 00"
DIAL = "0"
ANSWER = "Завтрак с 7 до 10."


@pytest.fixture
async def hotel(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Отель с ботом: staff-чат задан — у эскалации есть адресат (spec 0051)."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "-1001")
    get_settings.cache_clear()
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            demo_tenant,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=PHONE,
                reception_room_dial=DIAL,
            ),
        )
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("web", "stay-1")
    yield demo_tenant, conversation_id
    get_settings.cache_clear()


def _break(monkeypatch: pytest.MonkeyPatch, target: object, name: str) -> None:
    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("db blip")

    monkeypatch.setattr(target, name, broken)


def _llm_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**_: Any) -> OrchestratorTurn:
        raise AppError(code="ERR-AI-001", message="provider timeout", status_code=503)

    monkeypatch.setattr(orchestrator, "handle_message", fail)


async def _guest_says(tenant_id: uuid.UUID, conversation_id: uuid.UUID, text: str) -> list[str]:
    replies: list[str] = []

    async def reply(reply_text: str) -> None:
        replies.append(reply_text)

    with tenant_context(tenant_id):
        await run_guest_turn(
            conversation_id,
            text,
            uuid.uuid4(),
            external_id="stay-1",
            rate_limit_key="stay-1",
            reply=reply,
            provider=None,
            verified_room_number="305",
        )
    return replies


async def _escalation_reasons(tenant_id: uuid.UUID) -> list[str]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(ConversationEscalation.reason))
            return list(rows)


@pytest.mark.parametrize(
    ("target", "name"),
    [
        # Модель или инструмент упали не `AppError` (баг, сбой БД внутри шага).
        (orchestrator, "handle_message"),
        # Шаг хода ДО модели: чтение истории.
        (guest_turn, "load_dialog_history"),
        # Шаг хода ПОСЛЕ модели: гейт подтверждения не записался.
        (guest_turn, "set_pending_action"),
    ],
)
async def test_unexpected_failure_before_reply_degrades(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: object,
    name: str,
) -> None:
    """Любой шаг упал до реплики: гость слышит «зову сотрудника», и это правда."""
    tenant_id, conversation_id = hotel
    if name == "set_pending_action":

        async def answered(**_: Any) -> OrchestratorTurn:
            return OrchestratorTurn(kind=TurnKind.REPLY, reply_text=ANSWER)

        monkeypatch.setattr(orchestrator, "handle_message", answered)
    _break(monkeypatch, target, name)
    configure_logging()

    replies = await _guest_says(tenant_id, conversation_id, "во сколько завтрак?")

    assert replies == [DEGRADED_REPLY]
    assert await _escalation_reasons(tenant_id) == ["llm_unavailable"]
    record = find_record(read_log_records(capsys), "guest_turn_failed")
    assert record["error_code"] == ERR_GUEST_TURN_FAILED
    assert record["level"] == "error"
    assert record["error_type"] == "RuntimeError"


async def test_failed_escalation_sends_guest_to_reception(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Провайдер лёг, и эскалация не записалась: обещать сотрудника нельзя
    (spec 0051), но и молчать нельзя — гостя ведут на ресепшен."""
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    _break(monkeypatch, guest_turn, "escalate")
    configure_logging()

    replies = await _guest_says(tenant_id, conversation_id, "алло")

    assert replies == [no_staff_reply(PHONE, DIAL)]
    record = find_record(read_log_records(capsys), "guest_turn_escalation_failed")
    assert record["level"] == "error"


async def test_reception_text_survives_unreadable_config(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ход, эскалация и чтение конфига упали — текст «на ресепшен» без цифр
    действует и так: молчание хуже неполного текста."""
    tenant_id, conversation_id = hotel
    _break(monkeypatch, orchestrator, "handle_message")
    _break(monkeypatch, guest_turn, "escalate")
    _break(monkeypatch, guest_turn, "load_tenant_config")

    replies = await _guest_says(tenant_id, conversation_id, "алло")

    assert replies == [no_staff_reply(None, None)]


async def test_failure_after_reply_is_not_followed_by_second_reply(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Реплика ушла, упала её запись: вторая реплика «не могу ответить»
    соврала бы — исключение летит дальше, как до страховки."""
    tenant_id, conversation_id = hotel

    async def answered(**_: Any) -> OrchestratorTurn:
        return OrchestratorTurn(kind=TurnKind.REPLY, reply_text=ANSWER)

    monkeypatch.setattr(orchestrator, "handle_message", answered)
    sent: list[str] = []

    async def reply_then_fail(text: str) -> None:
        sent.append(text)
        raise RuntimeError("record failed after send")

    with tenant_context(tenant_id), pytest.raises(RuntimeError, match="after send"):
        await run_guest_turn(
            conversation_id,
            "во сколько завтрак?",
            uuid.uuid4(),
            external_id="stay-1",
            rate_limit_key="stay-1",
            reply=reply_then_fail,
            provider=None,
        )

    assert sent == [ANSWER]
    assert await _escalation_reasons(tenant_id) == []

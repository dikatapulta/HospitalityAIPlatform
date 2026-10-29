"""Эскалация без адресата: гостю не обещают сотрудника (spec 0051, issue #393).

Адресат — карточка в кабинете (новая или невзятая прежняя) или staff-чат
Telegram. Нет ни того, ни другого — любая реплика-обещание хода заменяется
текстом «позвоните на ресепшен»; факт эскалации публикуется всё равно.
Ход гостя зовётся напрямую, исход оркестратора подменяется — как в
`test_escalation_requests.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.channels.common.guest_turn import DEGRADED_REPLY, no_staff_reply, run_guest_turn
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
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
MODEL_PROMISE = "Сейчас позову сотрудника — он с вами свяжется."


async def _configure(tenant_id: uuid.UUID, escalation_category_key: str | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=PHONE,
                reception_room_dial=DIAL,
                escalation_category_key=escalation_category_key,
            ),
        )


@pytest.fixture
async def hotel(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Отель гостевой копии до этапа 3: ни бота, ни службы эскалаций."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "")
    get_settings.cache_clear()
    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
    await _configure(demo_tenant, None)
    yield demo_tenant, conversation_id


def _llm_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**_: Any) -> OrchestratorTurn:
        raise AppError(code="ERR-AI-002", message="daily budget exhausted", status_code=429)

    monkeypatch.setattr(orchestrator, "handle_message", fail)


def _guest_asks_for_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель позвала сотрудника инструментом и пообещала его своими словами."""

    async def turn(**_: Any) -> OrchestratorTurn:
        return OrchestratorTurn(
            kind=TurnKind.NEEDS_HUMAN,
            reply_text=MODEL_PROMISE,
            escalation=EscalationContext(
                reason=EscalationReason.GUEST_REQUESTED_STAFF,
                error_code="ERR-AI-005",
                action_summary="гость просит менеджера",
            ),
        )

    monkeypatch.setattr(orchestrator, "handle_message", turn)


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


async def _escalation_count(tenant_id: uuid.UUID) -> int:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            count = await session.scalar(select(func.count()).select_from(ConversationEscalation))
    return int(count or 0)


def test_no_staff_reply_follows_the_hotel_settings() -> None:
    """Цифра и телефон — из конфига; без них фраза остаётся действенной (§3)."""
    full = no_staff_reply(PHONE, DIAL)
    assert "наберите 0 с телефона в номере" in full
    assert "dial 0 from your room phone" in full
    assert full.endswith(f"\n📞 Ресепшен / Reception: {PHONE}")

    bare = no_staff_reply(None, None)
    assert "наберите" not in bare
    assert "📞" not in bare
    assert "Позвоните на ресепшен с телефона в номере или подойдите к стойке." in bare


@pytest.mark.parametrize("text", [no_staff_reply(PHONE, DIAL), no_staff_reply(None, None)])
def test_no_staff_reply_promises_no_one(text: str) -> None:
    """Смысл текста — не обещать человека: ни «зову», ни «подключу», ни «calling»."""
    for promise in ("зову", "подключу", "сотрудник", "calling", "staff member"):
        assert promise not in text


async def test_llm_outage_without_addressee_sends_guest_to_reception(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Исчерпан дневной потолок у копии без бота и службы: вместо «уже зову
    сотрудника» — ресепшен с цифрой и телефоном отеля; эскалация записана."""
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    configure_logging()

    replies = await _guest_says(tenant_id, conversation_id, "где мой трансфер?")

    assert replies == [no_staff_reply(PHONE, DIAL)]
    assert await _escalation_count(tenant_id) == 1
    record = find_record(read_log_records(capsys), "escalation_without_staff")
    assert record["reason"] == "llm_unavailable"
    assert record["level"] == "info"


async def test_model_promise_is_replaced_without_addressee(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обещание модели из `escalate_to_staff` гостю не уходит: звать некому."""
    tenant_id, conversation_id = hotel
    _guest_asks_for_staff(monkeypatch)

    replies = await _guest_says(tenant_id, conversation_id, "позовите менеджера")

    assert replies == [no_staff_reply(PHONE, DIAL)]
    assert await _escalation_count(tenant_id) == 1


async def test_staff_chat_is_an_addressee(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отель с ботом: сообщение уйдёт в staff-чат — обещание правдиво (spec 0022)."""
    tenant_id, conversation_id = hotel
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "-1001")
    get_settings.cache_clear()
    _llm_down(monkeypatch)
    assert await _guest_says(tenant_id, conversation_id, "алло") == [DEGRADED_REPLY]

    _guest_asks_for_staff(monkeypatch)
    assert await _guest_says(tenant_id, conversation_id, "позовите менеджера") == [MODEL_PROMISE]


async def test_waiting_card_is_an_addressee(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отель «только кабинет»: и новая карточка, и невзятая прежняя (вторая
    эскалация гаснет дедупликацией, spec 0039 §2.5) — адресат есть."""
    tenant_id, conversation_id = hotel
    await _configure(tenant_id, "reception")
    _llm_down(monkeypatch)

    first = await _guest_says(tenant_id, conversation_id, "алло")
    second = await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    assert first == second == [DEGRADED_REPLY]
    with tenant_context(tenant_id):
        page = await requests_api.list_requests(limit=50, offset=0)
    assert len(page.items) == 1

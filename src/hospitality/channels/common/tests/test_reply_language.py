"""Язык ответа гостю — сверка алфавитов, строка и счётчик (spec 0035 §8.2, #1043 п. 1).

Сверка — чистая функция и проверяется таблицей случаев, включая статические
трёхъязычные тексты: на них сигнал обязан молчать. Запись — через `run_guest_turn`,
тем путём, которым ходит живой гость; сбой записи не отнимает у гостя реплику.
Число за окно — на двух тенантах (P-4).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from structlog.testing import capture_logs

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common import reply_language
from hospitality.channels.common.guest_turn import (
    DEGRADED_REPLY,
    UNSUPPORTED_REPLY,
    run_guest_turn,
)
from hospitality.channels.common.models import GuestReplyLanguageCheck
from hospitality.channels.common.reply_language import (
    ReplyLanguageCount,
    check_reply_language,
    compare_reply_language,
    count_reply_language,
)
from hospitality.channels.common.store import ensure_conversation, insert_inbound_message
from hospitality.shared.alphabet import Alphabet
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

ENGLISH_GUEST = "What time is breakfast?"
RUSSIAN_REPLY = "Завтрак с 07:00 до 10:30 в ресторане на первом этаже."
ENGLISH_REPLY = "Breakfast is served from 07:00 to 10:30 on the ground floor."


@pytest.mark.parametrize(
    ("guest", "reply", "expected"),
    [
        (ENGLISH_GUEST, RUSSIAN_REPLY, (Alphabet.LATIN, Alphabet.CYRILLIC)),
        (ENGLISH_GUEST, ENGLISH_REPLY, (Alphabet.LATIN, Alphabet.LATIN)),
        ("Во сколько завтрак?", ENGLISH_REPLY, (Alphabet.CYRILLIC, Alphabet.LATIN)),
        # Казахский и русский — оба кириллица: подмену языка сверка не видит (§8.2).
        ("Таңғы ас қашан басталады?", RUSSIAN_REPLY, (Alphabet.CYRILLIC, Alphabet.CYRILLIC)),
        ("早餐几点开始？我在三零五房间住宿", RUSSIAN_REPLY, (Alphabet.OTHER, Alphabet.CYRILLIC)),
        # Русское название в английском ответе — всё ещё латиница.
        (
            ENGLISH_GUEST,
            "Breakfast is in the restaurant «Достык» on the ground floor.",
            (Alphabet.LATIN, Alphabet.LATIN),
        ),
        # Коротко — алфавит гостя не решён: русский гость пишет «wifi» латиницей.
        ("wifi?", RUSSIAN_REPLY, None),
        ("305", RUSSIAN_REPLY, None),
        # Трёхъязычные статические тексты — смесь, ни на каком одном алфавите.
        (ENGLISH_GUEST, DEGRADED_REPLY, None),
        (ENGLISH_GUEST, UNSUPPORTED_REPLY, None),
        (ENGLISH_GUEST, orchestrator._ESCALATION_TEXT, None),
        (ENGLISH_GUEST, orchestrator._DECLINED_TEXT, None),
    ],
)
def test_compare_reply_language(
    guest: str, reply: str, expected: tuple[Alphabet, Alphabet] | None
) -> None:
    check = compare_reply_language(guest, reply)
    if expected is None:
        assert check is None
    else:
        assert check is not None
        assert (check.guest, check.reply) == expected
        assert check.mismatch is (expected[0] is not expected[1])


async def _turn(tenant_id: uuid.UUID, guest_text: str, model_reply: str) -> list[str]:
    """Один ход живого гостя через `run_guest_turn`; вернуть отправленное гостю."""
    sent: list[str] = []

    async def reply(text: str) -> None:
        sent.append(text)

    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", "4242")
        message_id = await insert_inbound_message(
            conversation_id,
            NormalizedMessage(
                channel="telegram",
                chat_id="4242",
                kind=MessageKind.TEXT,
                text=guest_text,
                idempotency_key=f"telegram:update:{uuid.uuid4().hex}",
                external_message_id="1",
            ),
            correlation_id="test",
        )
        assert message_id is not None
        await run_guest_turn(
            conversation_id,
            guest_text,
            message_id,
            external_id="4242",
            rate_limit_key="4242",
            reply=reply,
            provider=ScriptedLlmProvider([MockTurn(text=model_reply)]),
        )
    return sent


async def _rows(tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            result = await session.execute(
                select(
                    GuestReplyLanguageCheck.guest_alphabet, GuestReplyLanguageCheck.reply_alphabet
                )
            )
            return [(guest, reply) for guest, reply in result.all()]


def _metric(tenant_id: uuid.UUID, result: str) -> float:
    sample = {"tenant_id": str(tenant_id), "result": result}
    return REGISTRY.get_sample_value("guest_reply_language_checks_total", sample) or 0.0


async def test_turn_records_a_mismatch_without_touching_the_reply(demo_tenant: uuid.UUID) -> None:
    before = _metric(demo_tenant, "mismatch")
    with capture_logs() as logs:
        sent = await _turn(demo_tenant, ENGLISH_GUEST, RUSSIAN_REPLY)

    assert sent == [RUSSIAN_REPLY]  # сигнал, а не гейт: ответ ушёл как есть
    assert await _rows(demo_tenant) == [("latin", "cyrillic")]
    assert _metric(demo_tenant, "mismatch") == before + 1
    mismatch = [log for log in logs if log["event"] == "guest_reply_language_mismatch"]
    assert len(mismatch) == 1
    # Текст гостя и ответа в лог не попадает (PII_REGISTRY).
    assert "breakfast" not in str(mismatch[0]).lower()


async def test_turn_records_a_match_and_skips_an_unclear_guest(demo_tenant: uuid.UUID) -> None:
    await _turn(demo_tenant, ENGLISH_GUEST, ENGLISH_REPLY)
    await _turn(demo_tenant, "ok", RUSSIAN_REPLY)

    assert await _rows(demo_tenant) == [("latin", "latin")]


async def test_broken_write_keeps_the_turn(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> object:
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(reply_language, "session_scope", broken)
    before = _metric(demo_tenant, "failed")
    with capture_logs() as logs:
        sent = await _turn(demo_tenant, ENGLISH_GUEST, RUSSIAN_REPLY)

    assert sent == [RUSSIAN_REPLY]
    assert _metric(demo_tenant, "failed") == before + 1
    assert any(log["event"] == "guest_reply_language_record_failed" for log in logs)


async def test_count_is_per_tenant_and_per_window(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    hotel_a, hotel_b = two_tenants
    conversation = uuid.uuid4()
    with tenant_context(hotel_a):
        await check_reply_language(conversation, ENGLISH_GUEST, RUSSIAN_REPLY)
        await check_reply_language(conversation, ENGLISH_GUEST, ENGLISH_REPLY)
    with tenant_context(hotel_b):
        await check_reply_language(conversation, ENGLISH_GUEST, RUSSIAN_REPLY)

    now = utc_now()
    window = {"created_after": now - timedelta(hours=1), "created_before": now + timedelta(hours=1)}
    with tenant_context(hotel_a):
        assert await count_reply_language(**window) == ReplyLanguageCount(checked=2, mismatched=1)
        later = {
            "created_after": now + timedelta(hours=1),
            "created_before": now + timedelta(days=1),
        }
        assert await count_reply_language(**later) == ReplyLanguageCount(checked=0, mismatched=0)
    with tenant_context(hotel_b):
        assert await count_reply_language(**window) == ReplyLanguageCount(checked=1, mismatched=1)

"""Данные о проживании в прогоне ретеншна и запись о согласии диалога (spec 0032 §6, #948).

Прогон целиком (`enforce_guest_text_retention`) — так его зовёт воркер. Сами
правила удаления Stay и гостей проверяет модуль guests
(`modules/guests/tests/test_stay_retention.py`); здесь — то, что добавляет
канальное ядро: счётчики прогона, ссылка диалога на удалённую идентичность и
запись о согласии, которую пишет `store.record_consent`.

Таблицы модуля guests читаются SQL'ом: его модели — приватная часть модуля.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common import store
from hospitality.channels.common.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
)
from hospitality.channels.common.retention import enforce_guest_text_retention
from hospitality.channels.common.store import (
    ensure_conversation,
    insert_inbound_message,
    record_consent,
)
from hospitality.modules.guests import api as guests_api
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

RETENTION_DAYS = 90
OLD = 91
VERSION = "2026-07-29-v3"


def _days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=days)


async def _expired_stay(
    tenant_id: uuid.UUID,
    *,
    kind: guests_api.GuestIdentityKind = guests_api.GuestIdentityKind.WEB,
    external_id: str | None = None,
) -> guests_api.GuestSessionGrant:
    """Проживание с привязанным гостем, «выехавшим» 91 день назад."""
    with tenant_context(tenant_id):
        checked_in = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="305", check_out_at=utc_now() + timedelta(days=1))
        )
        grant = await guests_api.start_guest_session(
            guests_api.GuestSessionStart(
                room_number="305",
                code=checked_in.access_code,
                identity_kind=kind,
                identity_external_id=external_id or str(uuid.uuid4()),
                consent_version=VERSION,
            )
        )
        assert grant is not None
        await guests_api.check_out(checked_in.stay.id)
        async with session_scope() as session:
            await session.execute(
                text("UPDATE stays SET check_out_at = :at"), {"at": _days_ago(OLD)}
            )
            await session.execute(
                text("UPDATE guests SET created_at = :at"), {"at": _days_ago(OLD + 1)}
            )
    return grant


async def _fresh_message(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    content_kind=MessageContentKind.TEXT,
                    text="документы готовы",
                    correlation_id="test",
                )
            )


async def _rows(tenant_id: uuid.UUID, sql: str) -> list[Any]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list((await session.execute(text(sql))).all())


async def test_run_deletes_stay_data_and_keeps_the_consent_record(
    demo_tenant: uuid.UUID,
) -> None:
    """Выезд старше срока: прогон удаляет Stay и гостя и называет их в счётчиках;
    запись о согласии остаётся. Повторный прогон — нули."""
    await _expired_stay(demo_tenant)

    first = await enforce_guest_text_retention(RETENTION_DAYS)
    second = await enforce_guest_text_retention(RETENTION_DAYS)

    assert (first.stays_deleted, first.guests_deleted, first.consent_records_deleted) == (1, 1, 0)
    assert (second.stays_deleted, second.guests_deleted) == (0, 0)
    assert await _rows(demo_tenant, "SELECT id FROM stays") == []
    assert await _rows(demo_tenant, "SELECT id FROM guest_sessions") == []
    records = await _rows(demo_tenant, "SELECT channel, room_number FROM consent_records")
    assert [(row.channel, row.room_number) for row in records] == [("web", "305")]


async def test_dialog_of_deleted_identity_loses_the_link_but_not_its_age(
    demo_tenant: uuid.UUID,
) -> None:
    """Веб-диалог, переживший проживание (свежее сообщение после выезда): ссылка на
    удалённую идентичность обнулена, `updated_at` прежний — обнуление не продлевает
    диалогу жизнь."""
    grant = await _expired_stay(demo_tenant)
    identity = grant.guest_identity_id
    updated_at = _days_ago(30)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation(
            "web", str(identity), guest_identity_id=identity
        )
        async with session_scope() as session:
            await session.execute(
                text("UPDATE conversations SET updated_at = :at"), {"at": updated_at}
            )
    await _fresh_message(demo_tenant, conversation_id)

    await enforce_guest_text_retention(RETENTION_DAYS)

    with tenant_context(demo_tenant):
        async with session_scope() as session:
            conversation = await session.get(Conversation, conversation_id)
    assert conversation is not None
    assert conversation.guest_identity_id is None
    assert conversation.updated_at == updated_at


async def test_telegram_dialog_outlives_the_guest_and_takes_messages(
    demo_tenant: uuid.UUID,
) -> None:
    """Чат Telegram гостя, чьё проживание удалено: привязки больше нет, диалог
    жив и принимает следующее сообщение."""
    chat_id = "700100200"
    telegram = guests_api.GuestIdentityKind.TELEGRAM
    await _expired_stay(demo_tenant, kind=telegram, external_id=chat_id)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", chat_id)
    await _fresh_message(demo_tenant, conversation_id)

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.guests_deleted == 1
    with tenant_context(demo_tenant):
        assert await guests_api.resolve_identity_session(telegram, chat_id) is None
        assert await ensure_conversation("telegram", chat_id) == conversation_id
        stored = await insert_inbound_message(
            conversation_id,
            NormalizedMessage(
                channel="telegram",
                chat_id=chat_id,
                kind=MessageKind.TEXT,
                text="добрый день",
                external_message_id="1",
                idempotency_key="telegram:update:1",
            ),
            "test",
        )
    assert stored is not None


async def test_dialog_consent_writes_a_record_without_stay(demo_tenant: uuid.UUID) -> None:
    """Consent-gate Telegram: запись о согласии — канал диалога, версия и тот же
    момент, что на диалоге; комнаты и дат нет — Stay ещё не привязан."""
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "700100200")
        await record_consent(conversation_id, VERSION)
        async with session_scope() as session:
            consent_at = await session.scalar(select(Conversation.consent_at))

    records = await _rows(
        demo_tenant,
        "SELECT channel, consent_at, consent_version, room_number, check_in_at, check_out_at "
        "FROM consent_records",
    )
    assert [tuple(row) for row in records] == [("telegram", consent_at, VERSION, None, None, None)]


async def test_no_consent_on_dialog_when_the_record_fails(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запись-доказательство пишется первой: упала — согласия на диалоге нет, и
    гость увидит экран согласия снова. Согласия без доказательства не бывает."""

    async def failing(**_: object) -> None:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(guests_api, "record_consent_evidence", failing)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "700100200")
        with pytest.raises(RuntimeError):
            await record_consent(conversation_id, VERSION)
        assert await store.load_consent_version(conversation_id) is None

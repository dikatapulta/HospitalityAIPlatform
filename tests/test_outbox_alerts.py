"""Алерт о событиях в dead-letter (issue #133, ADR-015).

Проверяется обещание Gate B «уведомление службе не теряется молча»: событие,
исчерпавшее повторы, доходит до человека сообщением ровно один раз, а провал
отправки не даёт пометить его рассказанным. Отправитель подставляется списком —
сети в тестах нет (канон `AlertSender`).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
from sqlalchemy import select

from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import (
    DomainEvent,
    OutboxEvent,
    deliver_pending_events,
    publish,
    subscribe,
)
from hospitality.shared.outbox_alerts import (
    alert_dead_letter_events,
    build_alert_text,
)
from hospitality.shared.tenancy import tenant_context


class AlertedNote(DomainEvent):
    """Тестовое событие, доставка которого всегда падает."""

    event_name: ClassVar[str] = "test.alerted_note"

    note: str


async def _poison_handler(event: AlertedNote) -> None:
    raise RuntimeError("staff chat not configured")


@pytest.fixture
async def tenant_id(canonical_database: None) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-dead-letter", name="Hotel Dead Letter")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _bury(tenant: uuid.UUID, notes: list[str]) -> None:
    """Опубликовать события и довести их до dead-letter (предел попыток — 1)."""
    subscribe(AlertedNote, _poison_handler)
    for note in notes:
        with tenant_context(tenant):
            async with session_scope() as session:
                await publish(session, AlertedNote(note=note))
    delivered = await deliver_pending_events(max_attempts=1, backoff_base_seconds=0)
    assert delivered == len(notes)


async def _dead_letter_rows() -> list[OutboxEvent]:
    async with platform_session_scope() as session:
        return list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.dead_lettered_at.is_not(None))
                    .order_by(OutboxEvent.dead_lettered_at)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# Текст алерта (§10 п.8: код, тенант, correlation id, детали, runbook)
# ---------------------------------------------------------------------------


def _fake_event(*, note: str, attempts: int, error: str | None) -> OutboxEvent:
    return OutboxEvent(
        id=uuid.UUID("0f1c9a2e-0000-0000-0000-000000000001"),
        tenant_id=uuid.UUID("8ab1c4d2-0000-0000-0000-000000000002"),
        event_name=note,
        payload={},
        correlation_id="7f3a2b1c",
        occurred_at=utc_now(),
        attempts=attempts,
        last_error=error,
        dead_lettered_at=utc_now(),
    )


def test_alert_text_carries_code_tenant_correlation_and_runbook() -> None:
    text = build_alert_text(
        [_fake_event(note="request.created", attempts=10, error="TelegramError: 400")],
        environment="staging",
        runbook_url="https://example.test/alerts.md",
    )

    assert text.startswith("🔴 [staging] ERR-EVENTS-002:")
    assert "1 событие в dead-letter" in text
    assert "request.created (id 0f1c9a2e, 10 попыток): TelegramError: 400" in text
    assert "tenant: 8ab1c4d2-0000-0000-0000-000000000002" in text
    assert "correlation_id: 7f3a2b1c" in text
    assert text.endswith("runbook: https://example.test/alerts.md#err-events-002")


def test_alert_text_counts_batch_and_describes_the_oldest() -> None:
    events = [
        _fake_event(note="request.created", attempts=10, error="boom"),
        _fake_event(note="request.completed", attempts=10, error="boom"),
        _fake_event(note="request.created", attempts=10, error="boom"),
    ]

    text = build_alert_text(events, environment="dev", runbook_url="https://example.test/a.md")

    assert "3 события в dead-letter. Старейшее — request.created" in text


def test_alert_text_survives_missing_diagnosis_and_trims_long_one() -> None:
    """`last_error` пуст (строка пришла из миграции) или огромен — текст остаётся читаемым."""
    empty = build_alert_text(
        [_fake_event(note="request.created", attempts=10, error=None)],
        environment="dev",
        runbook_url="https://example.test/a.md",
    )
    assert "причина не записана" in empty

    long = build_alert_text(
        [_fake_event(note="request.created", attempts=10, error="x" * 900)],
        environment="dev",
        runbook_url="https://example.test/a.md",
    )
    assert "…" in long and len(long) < 500


# ---------------------------------------------------------------------------
# Прогон: сказать один раз, не потерять при сбое отправки
# ---------------------------------------------------------------------------


async def test_dead_letter_is_reported_once(tenant_id: uuid.UUID) -> None:
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await _bury(tenant_id, ["lost-notification"])

    assert await alert_dead_letter_events(send) == 1
    assert len(sent) == 1
    assert "ERR-EVENTS-002" in sent[0]

    # Второй прогон молчит: строка помечена рассказанной — иначе алерт
    # повторялся бы каждую минуту до ручного разбора.
    assert await alert_dead_letter_events(send) == 0
    assert len(sent) == 1
    rows = await _dead_letter_rows()
    assert len(rows) == 1 and rows[0].dead_letter_alerted_at is not None


async def test_second_burial_is_reported_again(tenant_id: uuid.UUID) -> None:
    """Фикс не помог — вторые похороны рассказываются заново (ADR-015).

    Человек возвращает событие в очередь тремя полями (`attempts`,
    `next_attempt_at`, `dead_lettered_at` — ERR-EVENTS-002); пометку «о нём уже
    сказали» снимает сам код в момент похорон. Держи этот инвариант
    только текст runbook — забытое четвёртое поле хоронило бы событие молча,
    ровно тем отказом, против которого заведён issue #133.
    """
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await _bury(tenant_id, ["lost-notification"])
    assert await alert_dead_letter_events(send) == 1

    # Ручное восстановление по runbook: три поля, `dead_letter_alerted_at` не трогаем.
    async with platform_session_scope() as session:
        row = (await session.execute(select(OutboxEvent))).scalars().one()
        row.attempts = 0
        row.next_attempt_at = None
        row.dead_lettered_at = None
        assert row.dead_letter_alerted_at is not None

    # Фикс не помог: та же поломка хоронит событие второй раз.
    assert await deliver_pending_events(max_attempts=1, backoff_base_seconds=0) == 1

    assert await alert_dead_letter_events(send) == 1
    assert len(sent) == 2


async def test_quiet_when_nothing_is_buried(tenant_id: uuid.UUID) -> None:
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    assert await alert_dead_letter_events(send) == 0
    assert sent == []


async def test_failed_send_leaves_event_unreported(tenant_id: uuid.UUID) -> None:
    """Telegram лежит — пометка не ставится: пачка уйдёт следующим прогоном.

    Обратный порядок (пометить, потом отправить) хоронил бы событие молча —
    ровно тот отказ, против которого заведён issue #133.
    """
    attempts: list[str] = []

    async def broken_send(text: str) -> None:
        attempts.append(text)
        raise RuntimeError("telegram is down")

    await _bury(tenant_id, ["lost-notification"])

    with pytest.raises(RuntimeError):
        await alert_dead_letter_events(broken_send)

    rows = await _dead_letter_rows()
    assert len(rows) == 1 and rows[0].dead_letter_alerted_at is None

    async def good_send(text: str) -> None:
        attempts.append(text)

    assert await alert_dead_letter_events(good_send) == 1
    assert len(attempts) == 2


async def test_batch_is_capped_and_the_rest_waits_for_the_next_pass(
    tenant_id: uuid.UUID,
) -> None:
    """Сломанный обработчик хоронит события пачками: одно сообщение на прогон,
    остаток — следующим (лента алертов по событию — та же тишина, только громкая)."""
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await _bury(tenant_id, ["one", "two", "three"])

    assert await alert_dead_letter_events(send, batch_size=2) == 2
    assert "2 события в dead-letter" in sent[0]
    assert await alert_dead_letter_events(send, batch_size=2) == 1
    assert "1 событие в dead-letter" in sent[1]
    assert await alert_dead_letter_events(send, batch_size=2) == 0


async def test_default_batch_size_comes_from_worker_settings(tenant_id: uuid.UUID) -> None:
    """Без явного batch_size берётся `worker_batch_size` — канонический путь
    вызова из `run_worker` (симметрия с `deliver_pending_events`)."""
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await _bury(tenant_id, ["one", "two"])
    assert get_settings().worker_batch_size >= 2

    assert await alert_dead_letter_events(send) == 2
    assert len(sent) == 1

"""Срочная полоса воркера (issue #416): эскалация не ждёт общего цикла.

Общий цикл доставляет события по одному, и подписчик, который ждёт модель
(перевод заявки для службы, текст гостю о частичном выполнении), держал его до
конца всех попыток шлюза — эскалация «уже зову сотрудника» и сообщение о ЧП
стояли бы за ним.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from structlog.testing import capture_logs

from hospitality.ai.escalation import EscalationReason
from hospitality.channels.common.events import ConversationEscalated
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.platform.events import CanaryCreated
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.events import publish
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.publish_demo_event import publish_demo_event
from hospitality.worker import run_urgent_lane, run_worker

# Тест обязан упасть быстро, а не висеть, если полоса не работает.
GUARD_SECONDS = 10.0
STAFF_CHAT_ID = "-1001"


async def _publish_emergency_escalation() -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="urgent-lane-hotel", name="Urgent Lane Hotel")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await publish(
                session,
                ConversationEscalated(
                    conversation_id=uuid.uuid4(),
                    inbound_message_id=uuid.uuid4(),
                    chat_id="stay-1",
                    guest_message="пожар в коридоре",
                    reason=EscalationReason.EMERGENCY,
                    error_code="ERR-CHANNEL-005",
                ),
            )


async def test_escalation_reaches_staff_while_the_common_queue_is_stuck(
    canonical_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ядро #416: подписчик события в общей очереди завис, как перевод на
    «висящем» провайдере, а эскалация, опубликованная после него, доходит до
    чата персонала, не дожидаясь. До #416 она ждала бы его вечно."""
    release = asyncio.Event()

    async def stuck_subscriber(event: CanaryCreated) -> None:
        await release.wait()

    sender = RecordingSender()
    monkeypatch.setattr("hospitality.worker.echo_canary_created", stuck_subscriber)
    monkeypatch.setattr("hospitality.worker.build_telegram_sender", lambda settings: sender)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT_ID)
    get_settings.cache_clear()
    await publish_demo_event()  # первым в очереди — общий цикл встанет на нём
    await _publish_emergency_escalation()
    worker = asyncio.create_task(run_worker())
    try:
        async with asyncio.timeout(GUARD_SECONDS):
            while not sender.sent:
                await asyncio.sleep(0.05)
        assert not release.is_set() and not worker.done()  # общий цикл всё ещё стоит
    finally:
        release.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        get_settings.cache_clear()

    ((chat_id, text),) = sender.sent
    assert chat_id == STAFF_CHAT_ID
    assert "пожар в коридоре" in text


async def test_urgent_lane_survives_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-EVENTS-003 с `lane="urgent"`: сбой чтения outbox не убивает задачу полосы."""
    calls = 0

    async def broken_delivery(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("db is down")

    monkeypatch.setattr("hospitality.worker.deliver_pending_events", broken_delivery)
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "1e-6")
    get_settings.cache_clear()
    lane = asyncio.create_task(run_urgent_lane())
    try:
        with capture_logs() as logs:
            for _ in range(100):
                if calls >= 3:
                    break
                await asyncio.sleep(0)
        assert calls >= 3
        assert not lane.done()
        assert {log.get("lane") for log in logs if log["event"] == "worker_iteration_failed"} == {
            "urgent"
        }
    finally:
        lane.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lane
        get_settings.cache_clear()

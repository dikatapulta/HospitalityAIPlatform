"""Шаги ретеншна одного отеля — каждый в своём `try` (#1053 п. 5).

Упавший шаг одного раздела не оставляет гостевые тексты и данные о проживании
дольше срока: следующие шаги идут, сбой виден ERROR ERR-CHANNEL-004 с именем шага,
отель считается упавшим, пульс прогона не ставится. Данные о проживании ждут,
только если упал шаг, снимающий ссылки на Stay. Сценарии данных — помощники
`test_retention.py` и публичный API модуля guests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import NoReturn

import pytest
from sqlalchemy import text
from structlog.testing import capture_logs

from hospitality.channels.common.models import Message
from hospitality.channels.common.retention import (
    ERR_CHANNEL_RETENTION_FAILED,
    RETENTION_HEARTBEAT,
    enforce_guest_text_retention,
)
from hospitality.channels.common.tests.test_retention import (
    OLD,
    RETENTION_DAYS,
    _count,
    _make_conversation,
)
from hospitality.modules.guests import api as guests_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.heartbeat import read_heartbeat_age_seconds
from hospitality.shared.tenancy import tenant_context


async def _broken_step(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("relation does not exist")


async def _stays(tenant_id: uuid.UUID) -> int:
    """Число Stay отеля — SQL'ом: модели модуля guests каналам не видны (контракт R-5)."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return (await session.scalar(text("SELECT count(*) FROM stays"))) or 0


async def _old_text_and_stay(tenant_id: uuid.UUID) -> None:
    """Сообщение гостя и проживание, выехавшее за границей срока."""
    await _make_conversation(
        tenant_id, external_id="old", updated_days_ago=OLD, message_ages_days=(OLD,)
    )
    checked_out: datetime = utc_now() - timedelta(days=OLD)
    with tenant_context(tenant_id):
        stay = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="101", check_out_at=utc_now() + timedelta(days=1))
        )
        async with session_scope() as session:
            await session.execute(
                text("UPDATE stays SET check_out_at = :at WHERE id = :id"),
                {"at": checked_out, "id": stay.stay.id},
            )
            await session.execute(
                text("UPDATE guests SET created_at = :at WHERE id = :id"),
                {"at": checked_out - timedelta(days=1), "id": stay.stay.guest_id},
            )


async def test_failed_step_does_not_stop_the_steps_after_it(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Шаг «Наставника» падает: сообщение (шаг до него) и проживание (шаг после него)
    удалены; сбой назван по шагу, отель — упавший, пульса нет."""
    await _old_text_and_stay(demo_tenant)
    monkeypatch.setattr(mentor_api, "delete_expired_mentor_texts", _broken_step)

    with capture_logs() as logs:
        stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert await _count(demo_tenant, Message) == 0
    assert await _stays(demo_tenant) == 0
    assert (stats.messages_deleted, stats.stays_deleted) == (1, 1)
    assert (stats.steps_failed, stats.tenants_failed) == (1, 1)
    failures = [log for log in logs if log["event"] == "guest_text_retention_failed"]
    assert [(log["step"], log["error_code"], log["tenant_id"]) for log in failures] == [
        ("mentor", ERR_CHANNEL_RETENTION_FAILED, str(demo_tenant))
    ]
    assert await read_heartbeat_age_seconds(RETENTION_HEARTBEAT) is None


async def test_failed_stay_link_step_postpones_stay_data_to_the_next_run(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не снялись ссылки заказов на Stay — проживание ждёт: удалённый Stay оставил бы
    заказу идентификатор без строки. Следующий прогон без сбоя его удаляет."""
    await _old_text_and_stay(demo_tenant)
    with monkeypatch.context() as patch:
        patch.setattr(room_service_api, "detach_expired_orders", _broken_step)
        with capture_logs() as logs:
            stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert await _count(demo_tenant, Message) == 0  # тексты гостя — по сроку, как обычно
    assert await _stays(demo_tenant) == 1
    assert (stats.steps_failed, stats.tenants_failed, stats.stays_deleted) == (1, 1, 0)
    postponed = [log for log in logs if log["event"] == "guest_text_retention_stay_data_postponed"]
    assert [log["failed_steps"] for log in postponed] == [["room_service_orders"]]

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert (stats.steps_failed, stats.stays_deleted) == (0, 1)
    assert await _stays(demo_tenant) == 0
    assert await read_heartbeat_age_seconds(RETENTION_HEARTBEAT) is not None

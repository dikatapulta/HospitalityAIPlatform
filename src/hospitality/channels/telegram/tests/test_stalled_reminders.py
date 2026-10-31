"""Напоминание о взятой и не закрытой заявке (issue #120, spec 0028 §8).

Прогон целиком (`remind_stalled_requests`), как его зовёт воркер. Время взятия
двигается SQL'ом по `claimed_at` — канон `test_reminders.py`: ждать реальные
часы нельзя, а подменять «сейчас» — проверять не тот код.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.channels.telegram import stalled_reminders
from hospitality.channels.telegram.stalled_reminders import remind_stalled_requests
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

DEFAULT_CHAT = "999"


async def _configure(
    tenant_id: uuid.UUID,
    *,
    stalled_minutes: int | None = 240,
    stalled_by_category: dict[str, int] | None = None,
    staff_chats: dict[str, str] | None = None,
) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                staff_chats_by_category=staff_chats or {},
                request_stalled_after_minutes=stalled_minutes,
                request_stalled_minutes_by_category=stalled_by_category or {},
            ),
        )


async def _make_request(
    tenant_id: uuid.UUID,
    *,
    category_key: str = "maintenance",
    category_name: str = "Инженерия",
    summary: str = "не работает кондиционер",
    taken_minutes_ago: int | None = 300,
) -> requests_api.ServiceRequestRead:
    """Заявка, взятая `taken_minutes_ago` назад; None — оставить `new`."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get(category_key) or await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=category_key, name=category_name)
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number="305",
            )
        )
        if taken_minutes_ago is None:
            return request
        request = await requests_api.change_request_status(
            request.id, requests_api.RequestStatus.IN_PROGRESS
        )
        # Имя взявшего — тем же UPDATE: сотрудник из таблицы users тестам канала
        # не нужен, а карточке и тексту важна только строка имени.
        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE service_requests SET claimed_at = :at,"
                    " claimed_by_display_name = 'Айгерим' WHERE id = :id"
                ),
                {"at": utc_now() - timedelta(minutes=taken_minutes_ago), "id": request.id},
            )
        return request


async def _scan(sender: RecordingSender) -> int:
    return await remind_stalled_requests(sender=sender, default_staff_chat_id=DEFAULT_CHAT)


async def test_stalled_request_is_reminded_to_its_service_chat(demo_tenant: uuid.UUID) -> None:
    """DoD #120: взятая и не закрытая дольше срока — одно сообщение в чат службы."""
    await _configure(demo_tenant, staff_chats={"maintenance": "-100777"})
    request = await _make_request(demo_tenant, taken_minutes_ago=300)

    sender = RecordingSender()
    assert await _scan(sender) == 1

    chat_id, message = sender.sent[0]
    assert chat_id == "-100777"
    assert f"⏳ Заявка #{request.daily_number} в работе уже 5 ч и не закрыта" in message
    assert "Комната: 305" in message
    assert "Суть: не работает кондиционер" in message
    assert "Айгерим" not in message  # имя сотрудника в Telegram не уходит (PII)
    assert f"/done {request.daily_number}" in message
    # Кнопки статуса `in_progress`: закрыть можно прямо из напоминания.
    assert "Готово" in str(sender.markups[0])


async def test_off_by_default(demo_tenant: uuid.UUID) -> None:
    """По умолчанию выключено (решение основателя 27.07): поведение не меняется."""
    await _configure(demo_tenant, stalled_minutes=None)
    await _make_request(demo_tenant, taken_minutes_ago=5000)

    sender = RecordingSender()
    assert await _scan(sender) == 0
    assert sender.sent == []


async def test_counts_from_taking_not_from_creation(demo_tenant: uuid.UUID) -> None:
    """Отсчёт — от взятия: заявка создана давно, но взята только что — тишина."""
    await _configure(demo_tenant, stalled_minutes=60)
    request = await _make_request(demo_tenant, taken_minutes_ago=10)
    with tenant_context(demo_tenant):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE service_requests SET created_at = :at WHERE id = :id"),
                {"at": utc_now() - timedelta(hours=10), "id": request.id},
            )

    sender = RecordingSender()
    assert await _scan(sender) == 0


async def test_category_threshold_overrides_base(demo_tenant: uuid.UUID) -> None:
    """Свой срок у категории: F&B зовёт через 40 мин, инженерия на базовых 4 ч — нет."""
    await _configure(demo_tenant, stalled_minutes=240, stalled_by_category={"food": 40})
    await _make_request(
        demo_tenant,
        category_key="food",
        category_name="Ресторан",
        summary="ужин в номер",
        taken_minutes_ago=50,
    )
    await _make_request(demo_tenant, taken_minutes_ago=50)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert "ужин в номер" in sender.sent[0][1]


async def test_reminded_once(demo_tenant: uuid.UUID) -> None:
    """Одно напоминание на заявку (P-8): второй прогон молчит."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert await _scan(sender) == 0
    assert len(sender.sent) == 1


async def test_new_and_closed_requests_are_not_stalled(demo_tenant: uuid.UUID) -> None:
    """Невзятая — забота `reminders.py`, закрытая — ничья."""
    await _configure(demo_tenant, stalled_minutes=1)
    await _make_request(demo_tenant, taken_minutes_ago=None)
    done = await _make_request(demo_tenant, taken_minutes_ago=300)
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(done.id, requests_api.RequestStatus.DONE)

    sender = RecordingSender()
    assert await _scan(sender) == 0


async def test_send_failure_is_retried_next_scan(demo_tenant: uuid.UUID) -> None:
    """Сбой отправки не пишет ключ: следующий прогон повторит (канон пары)."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant)

    class FailingSender(RecordingSender):
        async def send_message(
            self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
        ) -> str | None:
            raise RuntimeError("Bot API down")

    assert await _scan(FailingSender()) == 0

    sender = RecordingSender()
    assert await _scan(sender) == 1


async def test_truncated_scan_still_reminds(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Срез упёрся в предел — прогон не падает и напоминает о попавших."""
    monkeypatch.setattr(stalled_reminders, "SCAN_LIMIT", 1)
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    await _make_request(demo_tenant, summary="мигает свет")

    sender = RecordingSender()
    assert await _scan(sender) == 1


async def test_tenant_isolation(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Скан одного отеля не видит заявок другого (P-4): у второго выключено."""
    first, second = two_tenants
    await _configure(first)
    await _configure(second, stalled_minutes=None)
    await _make_request(first)
    await _make_request(second)

    sender = RecordingSender()
    assert await _scan(sender) == 1


async def test_failure_on_one_tenant_does_not_stop_the_others(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой первого по порядку отеля логируется ERR-TELEGRAM-005, а скан
    доходит до следующего (канон пары)."""
    for tenant_id in two_tenants:
        await _configure(tenant_id)
        await _make_request(tenant_id)

    original = stalled_reminders._remind_tenant
    failed: list[uuid.UUID] = []

    async def first_one_fails(tenant_id: uuid.UUID, **kwargs: Any) -> tuple[int, int]:
        if not failed:
            failed.append(tenant_id)
            raise RuntimeError("connection reset")
        return await original(tenant_id, **kwargs)

    monkeypatch.setattr(stalled_reminders, "_remind_tenant", first_one_fails)

    sender = RecordingSender()
    assert await _scan(sender) == 1

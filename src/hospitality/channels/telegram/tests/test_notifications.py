"""Подписчики-уведомления Telegram (Task 0017, P-6, P-8, ADR-011).

Проверяет обработчики напрямую (в `tenant_context`, как их зовёт доставщик outbox):
идемпотентность при повторной доставке события и корректные пропуски (не-done,
заявка не из чата).
"""

from __future__ import annotations

import uuid

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.channels.telegram.notifications import (
    notify_guest_on_request_done,
    notify_staff_on_request_created,
)
from hospitality.channels.telegram.store import ensure_conversation, record_request_origin
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        return "m1"


async def _make_request(
    tenant_id: uuid.UUID, *, room_number: str | None = "305"
) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id, summary="убрать 305", room_number=room_number
            )
        )


async def test_staff_notification_is_idempotent(demo_tenant: uuid.UUID) -> None:
    """Повторная доставка request.created не шлёт второе уведомление службе (P-8)."""
    request = await _make_request(demo_tenant)
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=uuid.uuid4(), summary="убрать 305"
    )
    sender = RecordingSender()
    # Перевод — Fake-провайдер (суть уже по-русски → возвращаем как есть).
    translator = MockLlmProvider(text="убрать 305")
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
    assert len(sender.sent) == 1
    chat_id, text = sender.sent[0]
    assert chat_id == "999"
    assert "Суть: убрать 305" in text  # русская суть для персонала
    assert "Комната:" in text


async def test_staff_notification_translates_foreign_summary(demo_tenant: uuid.UUID) -> None:
    """Суть на языке гостя → персонал видит русский перевод + оригинал (баг #71).

    Китаец пишет по-китайски; персонал читает по-русски. Уведомление несёт русскую
    «Суть» (перевод) и строку «Гость написал» с оригиналом (эталон на случай осечки).
    """
    with tenant_context(demo_tenant):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id, summary="请打扫305房间", room_number="305"
            )
        )
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=category.id, summary="请打扫305房间"
    )
    sender = RecordingSender()
    translator = MockLlmProvider(text="Убрать номер 305")  # Fake «перевод на русский»
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
    _chat, text = sender.sent[0]
    assert "Суть: Убрать номер 305" in text  # русский перевод — персоналу
    assert "Гость написал: 请打扫305房间" in text  # оригинал — эталон
    assert "Комната: 305" in text


async def test_staff_notification_shows_room_number(demo_tenant: uuid.UUID) -> None:
    """Уведомление службе несёт номер комнаты — без него заявка неисполнима (S-1, #37).

    Событие `request.created` не несёт комнату; подписчик обязан дочитать заявку из
    БД (как `notify_guest_on_request_done`) и показать `room_number`.
    """
    # Комната (712) намеренно НЕ встречается в summary («убрать 305»): иначе тест
    # прошёл бы за счёт summary, не заметив, что room_number до службы не дошёл.
    request = await _make_request(demo_tenant, room_number="712")
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=request.category_id, summary=request.summary
    )
    sender = RecordingSender()
    # Fake-провайдер перевода: без него уведомление пошло бы в боевой Anthropic
    # (в CI ключа нет и не должно быть — тесты не ходят в сеть).
    translator = MockLlmProvider(text="убрать 305")
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
    assert len(sender.sent) == 1
    _, text = sender.sent[0]
    assert "712" in text


async def test_staff_notification_shows_daily_number(demo_tenant: uuid.UUID) -> None:
    """Уведомление службе несёт дневной номер `#N` и команды с ним (S-3, #38, заход 2а).

    Раньше в тексте был 36-символьный UUID; теперь — короткий `#N` в шапке и
    `/done N` в подсказке. Первая заявка дня → `#1`.
    """
    request = await _make_request(demo_tenant)
    assert request.daily_number == 1
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=request.category_id, summary=request.summary
    )
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(event, sender=sender, staff_chat_id="999")
    _, text = sender.sent[0]
    assert "#1" in text
    assert "/done 1" in text
    assert str(request.id) not in text  # длинного UUID в тексте больше нет (S-3)


async def test_staff_notification_omits_room_line_when_unknown(demo_tenant: uuid.UUID) -> None:
    """Заявка без комнаты (не из номера) → строки о комнате нет, не «Комната: None»."""
    request = await _make_request(demo_tenant, room_number=None)
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=request.category_id, summary=request.summary
    )
    sender = RecordingSender()
    translator = MockLlmProvider(text="убрать 305")
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
    assert len(sender.sent) == 1
    _, text = sender.sent[0]
    assert "None" not in text


async def test_staff_notification_skipped_without_chat(demo_tenant: uuid.UUID) -> None:
    """Staff-чат не настроен (пусто) → уведомление не шлётся, не падает."""
    event = requests_api.RequestCreated(
        request_id=uuid.uuid4(), category_id=uuid.uuid4(), summary="x"
    )
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(event, sender=sender, staff_chat_id="")
    assert sender.sent == []


async def test_guest_confirmation_is_idempotent(demo_tenant: uuid.UUID) -> None:
    """Повторная доставка request.status_changed(done) не шлёт второе подтверждение."""
    request = await _make_request(demo_tenant)
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("555")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        await notify_guest_on_request_done(event, sender=sender)
        await notify_guest_on_request_done(event, sender=sender)
    assert len(sender.sent) == 1
    chat_id, text = sender.sent[0]
    assert chat_id == "555"
    assert "убрать 305" in text


async def test_guest_confirmation_skipped_when_not_done(demo_tenant: uuid.UUID) -> None:
    """Не-финальный переход (например, assigned) не уведомляет гостя."""
    sender = RecordingSender()
    event = requests_api.RequestStatusChanged(
        request_id=uuid.uuid4(),
        old_status=requests_api.RequestStatus.NEW,
        new_status=requests_api.RequestStatus.ASSIGNED,
    )
    with tenant_context(demo_tenant):
        await notify_guest_on_request_done(event, sender=sender)
    assert sender.sent == []


async def test_guest_confirmation_skipped_without_origin(demo_tenant: uuid.UUID) -> None:
    """Заявка без привязки к диалогу (создана не из чата) → гость не уведомляется."""
    sender = RecordingSender()
    event = requests_api.RequestStatusChanged(
        request_id=uuid.uuid4(),
        old_status=requests_api.RequestStatus.IN_PROGRESS,
        new_status=requests_api.RequestStatus.DONE,
    )
    with tenant_context(demo_tenant):
        await notify_guest_on_request_done(event, sender=sender)
    assert sender.sent == []

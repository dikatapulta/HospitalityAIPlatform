"""Подписчики-уведомления Telegram (Task 0017, P-6, P-8, ADR-011).

Проверяет обработчики напрямую (в `tenant_context`, как их зовёт доставщик outbox):
идемпотентность при повторной доставке события и корректные пропуски (не-done,
заявка не из чата).
"""

from __future__ import annotations

import uuid
from typing import Any

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.channels.telegram.notifications import (
    notify_guest_on_request_closed,
    notify_staff_on_request_created,
)
from hospitality.channels.telegram.store import ensure_conversation, record_request_origin
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное и клавиатуры."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict[str, Any] | None] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return "m" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        return None


async def _make_request(
    tenant_id: uuid.UUID,
    *,
    room_number: str | None = "305",
    summary: str = "убрать 305",
    guest_language: str | None = None,
) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                summary=summary,
                room_number=room_number,
                guest_language=guest_language,
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
    БД (как `notify_guest_on_request_closed`) и показать `room_number`.
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
    translator = MockLlmProvider(text="убрать 305")
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
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
        await notify_guest_on_request_closed(event, sender=sender)
        await notify_guest_on_request_closed(event, sender=sender)
    assert len(sender.sent) == 1
    chat_id, text = sender.sent[0]
    assert chat_id == "555"
    assert "убрать 305" in text


async def test_guest_confirmation_skipped_when_not_done(demo_tenant: uuid.UUID) -> None:
    """Не-финальный переход (взяли в работу) не уведомляет гостя."""
    sender = RecordingSender()
    event = requests_api.RequestStatusChanged(
        request_id=uuid.uuid4(),
        old_status=requests_api.RequestStatus.NEW,
        new_status=requests_api.RequestStatus.IN_PROGRESS,
    )
    with tenant_context(demo_tenant):
        await notify_guest_on_request_closed(event, sender=sender)
    assert sender.sent == []


async def test_guest_done_message_is_single_language_russian(demo_tenant: uuid.UUID) -> None:
    """Русскоязычному гостю (и заявке без языка у тенанта с default ru) — чистый
    русский текст без «/ Your request is done» (spec 0021 П-1) и без вызова LLM."""
    request = await _make_request(demo_tenant)  # guest_language=None → default ru
    sender = RecordingSender()
    translator = MockLlmProvider(text="MUST NOT BE USED")
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("556")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        await notify_guest_on_request_closed(event, sender=sender, translate_provider=translator)
    (_, text) = sender.sent[0]
    assert "выполнена" in text
    assert "Your request" not in text  # двуязычной заглушки больше нет
    assert translator.calls == []  # русский — канонический текст, LLM не зовётся


async def test_guest_done_message_translated_to_guest_language(demo_tenant: uuid.UUID) -> None:
    """Заявка с guest_language=kk → гость получает перевод (один вызов, один язык)."""
    request = await _make_request(demo_tenant, summary="305 бөлмені тазалау", guest_language="kk")
    translated = "«305 бөлмені тазалау» өтініміңіз орындалды. Рақмет!"
    translator = MockLlmProvider(text=translated)
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("557")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        await notify_guest_on_request_closed(event, sender=sender, translate_provider=translator)
    (_, text) = sender.sent[0]
    assert text == translated
    # Провайдеру ушёл канонический русский текст с сутью гостя и целевым языком в системе.
    (call,) = translator.calls
    assert "305 бөлмені тазалау" in call.messages[0].content
    assert call.system is not None and '"kk"' in call.system


async def test_guest_message_degrades_to_canonical_on_translate_failure(
    demo_tenant: uuid.UUID,
) -> None:
    """Сбой перевода не съедает уведомление: уходит канонический русский текст (§7.8)."""
    request = await _make_request(demo_tenant, guest_language="zh")
    translator = MockLlmProvider(timeouts_before_success=99)  # провайдер всегда падает
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("558")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        await notify_guest_on_request_closed(event, sender=sender, translate_provider=translator)
    (_, text) = sender.sent[0]
    assert "выполнена" in text  # канонический текст, суть — словами гостя
    assert "убрать 305" in text


async def test_guest_notified_on_cancelled(demo_tenant: uuid.UUID) -> None:
    """Отменённая заявка больше не исчезает молча: гость получает сообщение об отмене
    (spec 0021 П-1), идемпотентно по собственному ключу."""
    request = await _make_request(demo_tenant)
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("559")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.NEW,
            new_status=requests_api.RequestStatus.CANCELLED,
        )
        await notify_guest_on_request_closed(event, sender=sender)
        await notify_guest_on_request_closed(event, sender=sender)
    assert len(sender.sent) == 1
    (_, text) = sender.sent[0]
    assert "отменить" in text
    assert "убрать 305" in text


async def test_guest_confirmation_skipped_without_origin(demo_tenant: uuid.UUID) -> None:
    """Заявка без привязки к диалогу (создана не из чата) → гость не уведомляется."""
    sender = RecordingSender()
    event = requests_api.RequestStatusChanged(
        request_id=uuid.uuid4(),
        old_status=requests_api.RequestStatus.IN_PROGRESS,
        new_status=requests_api.RequestStatus.DONE,
    )
    with tenant_context(demo_tenant):
        await notify_guest_on_request_closed(event, sender=sender)
    assert sender.sent == []


async def test_staff_notification_carries_inline_keyboard(demo_tenant: uuid.UUID) -> None:
    """Уведомление о новой заявке несёт кнопки статуса `new` (#38 п.2): «Взять в
    работу» с callback_data `req:<uuid>:start` — ноль ручного ввода."""
    request = await _make_request(demo_tenant)
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=request.category_id, summary=request.summary
    )
    sender = RecordingSender()
    translator = MockLlmProvider(text="убрать 305")
    with tenant_context(demo_tenant):
        await notify_staff_on_request_created(
            event, sender=sender, staff_chat_id="999", translate_provider=translator
        )
    (markup,) = sender.markups
    assert markup is not None
    assert f"req:{request.id}:start" in str(markup)
    assert "Взять в работу" in str(markup)


async def test_guest_done_message_includes_resolution_note(demo_tenant: uuid.UUID) -> None:
    """Примечание персонала доходит до гостя частью уведомления (spec 0021 П-4):
    «От персонала: …» — и переводится вместе со всем текстом одним вызовом."""
    request = await _make_request(demo_tenant)
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(request.id, requests_api.RequestStatus.IN_PROGRESS)
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.DONE,
            resolution_note="кофе закончился, принесём утром",
        )
        conversation_id = await ensure_conversation("560")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        await notify_guest_on_request_closed(event, sender=sender)
    (_, text) = sender.sent[0]
    assert "выполнена" in text
    assert "От персонала: кофе закончился, принесём утром" in text

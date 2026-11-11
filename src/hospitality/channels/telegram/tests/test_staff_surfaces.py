"""Мелочи чата службы из аудита 05.08 (issue #235).

Статус словами в ответах персоналу, кнопки напоминаний вслед за карточкой,
имя взявшего в сообщении об отмене гостем. Путь — настоящий, как в
`test_staff_card.py`: уведомление шлёт подписчик, переходы — `handle_staff_message`.
"""

from __future__ import annotations

import uuid

from hospitality.channels.common.store import ensure_conversation, record_outbound_message
from hospitality.channels.telegram import keyboards, reminders, stalled_reminders
from hospitality.channels.telegram.notifications import (
    notify_staff_on_request_cancelled_by_guest,
)
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.channels.telegram.tests.test_staff_card import (  # noqa: F401  (фикстура-реимпорт)
    CARD_ID,
    STAFF_CHAT,
    _command,
    _handle,
    _notified_request,
    _press,
    _staff_chat_is_default,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context

IN_PROGRESS = requests_api.RequestStatus.IN_PROGRESS


async def _reminded(tenant_id: uuid.UUID, request_id: uuid.UUID, prefix: str, sent_id: str) -> None:
    """Напоминание в чате службы — исходящее с ключом напоминания, как его пишет воркер."""
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await record_outbound_message(
            conversation_id,
            "⏰ Заявку никто не взял",
            "c1",
            external_message_id=sent_id,
            idempotency_key=f"{prefix}:{request_id}",
        )


async def test_staff_replies_name_status_in_words(demo_tenant: uuid.UUID) -> None:
    """Тост кнопки и ответ команде — «в работе», «выполнена частично», не `in_progress`."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    number = request.daily_number
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)
    await _handle(demo_tenant, _command(f"/done {number} полотенец нет"), sender)

    assert [text for _, text in sender.toasts] == [f"Заявка #{number} — в работе."]
    reply = sender.sent[-1][1]
    assert reply.startswith(f"Заявка #{number} «убрать 305» — выполнена частично.")
    assert "→" not in reply and "done" not in reply


async def test_card_press_moves_reminder_buttons_too(demo_tenant: uuid.UUID) -> None:
    """Кнопка под карточкой перерисовывает и кнопки обоих напоминаний; закрытие их снимает."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    await _reminded(demo_tenant, request.id, reminders.IDEMPOTENCY_KEY_PREFIX, "r1")
    await _reminded(demo_tenant, request.id, stalled_reminders.IDEMPOTENCY_KEY_PREFIX, "r2")

    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)
    in_progress = keyboards.keyboard_for_status(request.id, IN_PROGRESS)
    assert sender.keyboard_edits == [
        (STAFF_CHAT, "r1", in_progress),
        (STAFF_CHAT, "r2", in_progress),
    ]
    assert [edit[:2] for edit in sender.text_edits] == [(STAFF_CHAT, CARD_ID)]

    sender.keyboard_edits.clear()
    await _handle(demo_tenant, _command(f"/done {request.daily_number}"), sender)
    assert sender.keyboard_edits == [(STAFF_CHAT, "r1", None), (STAFF_CHAT, "r2", None)]


async def test_reminder_press_does_not_edit_its_buttons_twice(demo_tenant: uuid.UUID) -> None:
    """Нажатая кнопка под напоминанием — одна правка его кнопок, а не две."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    await _reminded(demo_tenant, request.id, reminders.IDEMPOTENCY_KEY_PREFIX, "r1")

    await _handle(
        demo_tenant, _press(request.id, keyboards.CallbackAction.START, under="r1"), sender
    )
    assert [edit[:2] for edit in sender.keyboard_edits] == [(STAFF_CHAT, "r1")]


async def _guest_cancels(
    tenant_id: uuid.UUID, sender: RecordingSender, *, claimed_by: str | None
) -> str:
    """Заявка (взятая в кабинете или нет) отменена гостем — текст в чат службы."""
    request = await _notified_request(tenant_id, sender)
    with tenant_context(tenant_id):
        if claimed_by is not None:
            async with platform_session_scope() as session:
                user = User(display_name=claimed_by)
                session.add(user)
                await session.flush()
                acting = requests_api.ActingUser(user_id=user.id, display_name=claimed_by)
            await requests_api.change_request_status(request.id, IN_PROGRESS, acting_user=acting)
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
        )
        await notify_staff_on_request_cancelled_by_guest(
            requests_api.RequestStatusChanged(
                request_id=request.id,
                old_status=IN_PROGRESS if claimed_by else requests_api.RequestStatus.NEW,
                new_status=requests_api.RequestStatus.CANCELLED,
                initiator=requests_api.RequestInitiator.GUEST,
            ),
            sender=sender,
            default_staff_chat_id=STAFF_CHAT,
        )
    return sender.sent[-1][1]


async def test_guest_cancel_names_who_took_it_in_cabinet(demo_tenant: uuid.UUID) -> None:
    """Взявший в кабинете видит своё имя второй строкой: идти уже не надо."""
    text = await _guest_cancels(demo_tenant, RecordingSender(), claimed_by="Айгерим")
    first, second, *_ = text.split("\n")
    assert first.startswith("🚫 Гость отменил заявку #")
    assert second == "Взял(а): Айгерим"


async def test_guest_cancel_without_claim_has_no_name_line(demo_tenant: uuid.UUID) -> None:
    """Никто не брал (или брали кнопкой в Telegram — имени нет) — строки нет."""
    text = await _guest_cancels(demo_tenant, RecordingSender(), claimed_by=None)
    assert "Взял(а)" not in text
    assert text.split("\n")[1].startswith("Категория: ")

"""Отмена заявки из staff-чата: причина списком (spec 0057 §2, PR 2 серии #54).

Кнопка «❌ Отменить» открывает список причин, причина отменяет заявку,
«Другое» спрашивает текст с ForceReply, `/cancel #N` без текста подсказывает.
Без HTTP: `handle_staff_message` напрямую, Bot API — фейк-отправитель.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.store import ensure_conversation, record_outbound_message
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"
NOTIFICATION_ID = "n1"
Reason = requests_api.CancelReason
Status = requests_api.RequestStatus


class RecordingSender:
    """Фейк порта TelegramSender: сообщения, кнопки, тосты и перерисовки."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict[str, Any] | None] = []
        self.toasts: list[str] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return "m" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.toasts.append(text)

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


async def _make_request(tenant_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    """Заявка с уведомлением в staff-чате — как после `notify_staff_on_request_created`."""
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="Принести 2 полотенца",
            )
        )
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await record_outbound_message(
            conversation_id,
            "🔔 Новая заявка",
            "c0",
            external_message_id=NOTIFICATION_ID,
            idempotency_key=f"staff:request_created:{request.id}",
        )
    return request


def _press(data: str) -> NormalizedMessage:
    """Нажатие кнопки под уведомлением (как отдаёт normalize_update)."""
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=f"callback:{uuid.uuid4()}",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.CALLBACK,
        text=data,
        reply_to=ReplyTo(external_message_id=NOTIFICATION_ID, text="🔔 Новая заявка"),
        callback_id=f"cb-{uuid.uuid4()}",
        actor_external_id="42",
    )


def _text(text: str, *, reply_to: ReplyTo | None = None) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=str(uuid.uuid4()),
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=text,
        reply_to=reply_to,
        actor_external_id="42",
    )


async def _run(tenant_id: uuid.UUID, message: NormalizedMessage) -> RecordingSender:
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def _stored(tenant_id: uuid.UUID, request_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        return await requests_api.get_request(request_id)


def _button_texts(markup: dict[str, Any] | None) -> list[str]:
    assert markup is not None
    return [button["text"] for row in markup["inline_keyboard"] for button in row]


def test_every_reason_button_fits_telegram_callback_limit() -> None:
    """Bot API отвергает всю клавиатуру, если хоть одна кнопка длиннее 64 байт."""
    markup = keyboards.cancel_reasons_keyboard(uuid.uuid4())
    rows: Any = markup["inline_keyboard"]
    for row in rows:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


@pytest.mark.parametrize("reason", keyboards.STAFF_CANCEL_REASONS)
def test_reason_callback_round_trips(reason: requests_api.CancelReason) -> None:
    request_id = uuid.uuid4()
    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.CANCEL_REASON, reason)
    assert keyboards.parse_callback_data(data) == keyboards.CallbackData(
        request_id, keyboards.CallbackAction.CANCEL_REASON, reason
    )


@pytest.mark.parametrize(
    "tail",
    ["cr", "cr:by_guest", "cr:whatever", "cancel:unavailable", "start:other"],
)
def test_malformed_reason_callback_is_rejected(tail: str) -> None:
    """Причина — только у `cr` и только из списка персонала: `by_guest` ставит домен."""
    assert keyboards.parse_callback_data(f"req:{uuid.uuid4()}:{tail}") is None


async def test_cancel_button_opens_reasons_without_cancelling(demo_tenant: uuid.UUID) -> None:
    request = await _make_request(demo_tenant)
    sender = await _run(
        demo_tenant,
        _press(keyboards.build_callback_data(request.id, keyboards.CallbackAction.CANCEL)),
    )

    assert (await _stored(demo_tenant, request.id)).status is Status.NEW
    ((chat_id, message_id, markup),) = sender.keyboard_edits
    assert (chat_id, message_id) == (STAFF_CHAT, NOTIFICATION_ID)
    assert _button_texts(markup) == [
        "Нет в наличии",
        "Гость отказался или уехал",
        "Дубль — такая заявка уже есть",
        "Отель такого не делает",
        "Другое — напишу сам",
        "← Назад",
    ]
    assert sender.toasts == [f"Заявка #{request.daily_number}: выберите причину отмены."]
    assert sender.sent == []  # меню — не событие для группы


async def test_back_restores_status_buttons(demo_tenant: uuid.UUID) -> None:
    request = await _make_request(demo_tenant)
    sender = await _run(
        demo_tenant,
        _press(keyboards.build_callback_data(request.id, keyboards.CallbackAction.BACK)),
    )

    assert (await _stored(demo_tenant, request.id)).status is Status.NEW
    ((_, _, markup),) = sender.keyboard_edits
    assert markup == keyboards.keyboard_for_status(request.id, Status.NEW)
    assert sender.toasts == [f"Заявка #{request.daily_number} не отменена."]
    assert sender.sent == []


async def test_cancel_button_on_closed_request_drops_buttons(demo_tenant: uuid.UUID) -> None:
    """Заявку закрыли в кабинете, а кнопки в чате старые: список не открывается."""
    request = await _make_request(demo_tenant)
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(
            request.id, Status.CANCELLED, cancel_reason=Reason.UNAVAILABLE
        )
    sender = await _run(
        demo_tenant,
        _press(keyboards.build_callback_data(request.id, keyboards.CallbackAction.CANCEL)),
    )

    assert sender.keyboard_edits == [(STAFF_CHAT, NOTIFICATION_ID, None)]
    assert sender.toasts == [f"Заявка #{request.daily_number} уже закрыта."]


@pytest.mark.parametrize(
    "reason", [Reason.UNAVAILABLE, Reason.GUEST_DECLINED, Reason.DUPLICATE, Reason.NOT_PROVIDED]
)
async def test_reason_button_cancels_with_that_reason(
    demo_tenant: uuid.UUID, reason: requests_api.CancelReason
) -> None:
    request = await _make_request(demo_tenant)
    data = keyboards.build_callback_data(request.id, keyboards.CallbackAction.CANCEL_REASON, reason)
    sender = await _run(demo_tenant, _press(data))

    stored = await _stored(demo_tenant, request.id)
    assert stored.status is Status.CANCELLED
    assert stored.cancel_reason is reason
    assert stored.resolution_note is None
    ((_, line),) = sender.sent
    assert f"Причина: {requests_api.CANCEL_REASON_LABELS[reason]}" in line
    assert sender.keyboard_edits == [(STAFF_CHAT, NOTIFICATION_ID, None)]  # терминал — без кнопок


async def test_other_asks_text_and_reply_cancels_with_it(demo_tenant: uuid.UUID) -> None:
    """«Другое — напишу сам» → вопрос с ForceReply; ответ — причина `other`, её прочтёт гость."""
    request = await _make_request(demo_tenant)
    data = keyboards.build_callback_data(
        request.id, keyboards.CallbackAction.CANCEL_REASON, Reason.OTHER
    )
    asked = await _run(demo_tenant, _press(data))

    assert (await _stored(demo_tenant, request.id)).status is Status.NEW
    ((_, question),) = asked.sent
    assert question == (
        f"Заявка #{request.daily_number}: почему отменяете? Гость прочитает ответ. "
        "Ответьте на это сообщение."
    )
    assert asked.markups == [{"force_reply": True}]
    assert asked.toasts == [f"Жду причину отмены #{request.daily_number} ответом на вопрос."]

    answer = _text("гостя нет в номере", reply_to=ReplyTo(external_message_id="m1", text=question))
    replied = await _run(demo_tenant, answer)

    stored = await _stored(demo_tenant, request.id)
    assert stored.status is Status.CANCELLED
    assert stored.cancel_reason is Reason.OTHER
    assert stored.resolution_note == "гостя нет в номере"
    ((_, line),) = replied.sent
    assert "Примечание: гостя нет в номере" in line and "Причина:" not in line


async def test_reply_to_done_question_still_completes(demo_tenant: uuid.UUID) -> None:
    """Оба вопроса кончаются «Ответьте на это сообщение»; переход — по ключу вопроса."""
    request = await _make_request(demo_tenant)
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(request.id, Status.IN_PROGRESS)
    asked = await _run(
        demo_tenant,
        _press(keyboards.build_callback_data(request.id, keyboards.CallbackAction.DONE_NOTE)),
    )
    ((_, question),) = asked.sent
    await _run(
        demo_tenant,
        _text("полотенец нет", reply_to=ReplyTo(external_message_id="m1", text=question)),
    )

    stored = await _stored(demo_tenant, request.id)
    assert stored.status is Status.DONE
    assert stored.cancel_reason is None


async def test_cancel_command_without_reason_hints_and_keeps_request(
    demo_tenant: uuid.UUID,
) -> None:
    request = await _make_request(demo_tenant)
    sender = await _run(demo_tenant, _text(f"/cancel {request.daily_number}"))

    assert (await _stored(demo_tenant, request.id)).status is Status.NEW
    ((_, hint),) = sender.sent
    assert hint == (
        f"Заявка #{request.daily_number}: выберите причину кнопкой «❌ Отменить» под "
        f"уведомлением — или напишите её словами: /cancel {request.daily_number} <причина>. "
        "Гость прочитает её."
    )


async def test_cancel_command_with_text_cancels_as_other(demo_tenant: uuid.UUID) -> None:
    request = await _make_request(demo_tenant)
    await _run(demo_tenant, _text(f"/cancel {request.daily_number} гостя нет в номере"))

    stored = await _stored(demo_tenant, request.id)
    assert stored.status is Status.CANCELLED
    assert stored.cancel_reason is Reason.OTHER
    assert stored.resolution_note == "гостя нет в номере"


async def test_cancel_command_as_reply_to_question_cancels(demo_tenant: uuid.UUID) -> None:
    """`/cancel текст` ответом на «почему отменяете?» — заявка из ключа вопроса."""
    request = await _make_request(demo_tenant)
    data = keyboards.build_callback_data(
        request.id, keyboards.CallbackAction.CANCEL_REASON, Reason.OTHER
    )
    asked = await _run(demo_tenant, _press(data))
    ((_, question),) = asked.sent
    await _run(
        demo_tenant,
        _text("/cancel дубль звонка", reply_to=ReplyTo(external_message_id="m1", text=question)),
    )

    stored = await _stored(demo_tenant, request.id)
    assert stored.status is Status.CANCELLED
    assert stored.cancel_reason is Reason.OTHER
    assert stored.resolution_note == "дубль звонка"

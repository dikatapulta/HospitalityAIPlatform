"""Карточка заявки в чате службы: одна заявка — одно сообщение (issue #92).

Уведомление о заявке перерисовывается при смене статуса (`editMessageText`),
строкой в группу уходит только итог закрытия. Путь — настоящий: уведомление шлёт
подписчик `request.created`, переходы — `handle_staff_message`, как вебхук.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import keyboards, staff_card
from hospitality.channels.telegram.notifications import notify_staff_on_request_created
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"
IN_PROGRESS = requests_api.RequestStatus.IN_PROGRESS
DONE = requests_api.RequestStatus.DONE
CARD_ID = "m1"  # фейк-отправитель отдаёт его первому send_message — уведомлению


@pytest.fixture(autouse=True)
def _staff_chat_is_default() -> Iterator[None]:
    """Дефолтный чат ведёт заявки всех служб (spec 0074 §4) — как в `test_staff.py`."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


class FailingTextSender(RecordingSender):
    """Bot API не принимает новый текст (длина, «не изменилось»)."""

    async def edit_message_text(
        self, chat_id: str, message_id: str, text: str, reply_markup: dict[str, Any] | None
    ) -> None:
        raise RuntimeError("Bad Request: message is not modified")


async def _notified_request(
    tenant_id: uuid.UUID, sender: RecordingSender, *, is_urgent: bool = False
) -> requests_api.ServiceRequestRead:
    """Заявка гостя и её уведомление в чате службы — настоящим подписчиком."""
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
                room_number="305",
                is_urgent=is_urgent,
            )
        )
        await notify_staff_on_request_created(
            requests_api.RequestCreated(
                request_id=request.id, category_id=category.id, summary=request.summary
            ),
            sender=sender,
            default_staff_chat_id=STAFF_CHAT,
            translate_provider=MockLlmProvider(text="убрать 305"),
        )
    return request


def _press(
    request_id: uuid.UUID, action: keyboards.CallbackAction, *, under: str = CARD_ID
) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=f"callback:{uuid.uuid4()}",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.CALLBACK,
        text=keyboards.build_callback_data(request_id, action),
        reply_to=ReplyTo(external_message_id=under, text="🔔 Новая заявка"),
        callback_id=f"cb-{uuid.uuid4()}",
        actor_external_id="42",
    )


def _command(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=str(uuid.uuid4()),
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=text,
    )


async def _handle(tenant_id: uuid.UUID, message: NormalizedMessage, sender: Any) -> None:
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")


async def test_new_card_is_the_notification_and_round_trips(demo_tenant: uuid.UUID) -> None:
    """Статус `new` — ровно прежнее уведомление; тело из него собирается обратно."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    ((_, sent),) = sender.sent
    number = request.daily_number
    assert sent == (
        f"🔔 Новая заявка #{number}\nКатегория: Уборка\nКомната: 305\nСуть: убрать 305\n\n"
        f"Ход: /start {number} (взять в работу) · /done {number} · /cancel {number}"
    )
    body = staff_card.body_of(sent)
    assert body == "Категория: Уборка\nКомната: 305\nСуть: убрать 305"
    assert staff_card.render(body, request) == sent


async def test_start_button_redraws_card_without_group_line(demo_tenant: uuid.UUID) -> None:
    """«Взять в работу» под карточкой: она перерисована целиком, в группе — ничего нового."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)

    number = request.daily_number
    assert len(sender.sent) == 1  # только само уведомление
    ((chat, message_id, card, markup),) = sender.text_edits
    assert (chat, message_id) == (STAFF_CHAT, CARD_ID)
    assert card == (
        f"🏃 Заявка #{number} — в работе\nКатегория: Уборка\nКомната: 305\nСуть: убрать 305\n\n"
        f"Ход: /done {number} · /cancel {number}"
    )
    assert markup == keyboards.keyboard_for_status(request.id, IN_PROGRESS)
    assert sender.keyboard_edits == []  # кнопки пришли вместе с текстом
    assert len(sender.toasts) == 1


async def test_done_button_closes_card_and_posts_outcome(demo_tenant: uuid.UUID) -> None:
    """«Готово»: карточка «выполнена» с «Вернуть в работу» (spec 0080), итог — строкой в группу."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.DONE), sender)

    *_, (_, _, card, markup) = sender.text_edits
    assert card.startswith(f"✅ Заявка #{request.daily_number} — выполнена\nКатегория: Уборка")
    assert card.endswith("Суть: убрать 305")  # ни «Ход:», ни итога
    assert markup == keyboards.keyboard_for_status(request.id, DONE, reopenable=True)
    assert len(sender.sent) == 2 and "— выполнена." in sender.sent[1][1]


async def test_partial_done_command_puts_note_on_card(demo_tenant: uuid.UUID) -> None:
    """`/done N <текст>`: карточка «выполнена частично» с примечанием; ответ команде — как был."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    number = request.daily_number
    await _handle(demo_tenant, _command(f"/start {number}"), sender)
    await _handle(demo_tenant, _command(f"/done {number} полотенец нет"), sender)

    *_, (_, _, card, markup) = sender.text_edits
    assert card.startswith(f"⚠️ Заявка #{number} — выполнена частично\n")
    assert card.endswith("Суть: убрать 305\n\nПримечание: полотенец нет")
    assert markup == keyboards.keyboard_for_status(request.id, DONE, reopenable=True)
    # Команда — разговор с ботом: ответ на неё приходит и на «в работу», и на закрытие.
    replies = [text for _, text in sender.sent[1:]]
    assert "— в работе." in replies[0] and "Остаток" in replies[1]


async def test_press_under_reminder_redraws_card_and_reminder_buttons(
    demo_tenant: uuid.UUID,
) -> None:
    """Кнопка под напоминанием (spec 0028): у него — только кнопки, карточка — своим чередом."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender)
    await _handle(
        demo_tenant, _press(request.id, keyboards.CallbackAction.START, under="r7"), sender
    )

    assert [edit[:2] for edit in sender.text_edits] == [(STAFF_CHAT, CARD_ID)]
    ((chat, message_id, markup),) = sender.keyboard_edits
    assert (chat, message_id) == (STAFF_CHAT, "r7")
    assert markup == keyboards.keyboard_for_status(request.id, IN_PROGRESS)


async def test_rejected_text_still_moves_buttons(demo_tenant: uuid.UUID) -> None:
    """Bot API не принял текст — кнопки всё равно под новый статус (как до #92)."""
    sender = FailingTextSender()
    request = await _notified_request(demo_tenant, sender)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)

    ((chat, message_id, markup),) = sender.keyboard_edits
    assert (chat, message_id) == (STAFF_CHAT, CARD_ID)
    assert markup is not None and "Готово" in str(markup)
    with tenant_context(demo_tenant):
        stored = await requests_api.get_request(request.id)
    assert stored.status is requests_api.RequestStatus.IN_PROGRESS


async def test_urgent_mark_stays_while_open(demo_tenant: uuid.UUID) -> None:
    """Срочность (spec 0034 §5) кричит, пока заявка открыта; у закрытой — шум."""
    sender = RecordingSender()
    request = await _notified_request(demo_tenant, sender, is_urgent=True)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.START), sender)
    await _handle(demo_tenant, _press(request.id, keyboards.CallbackAction.DONE), sender)

    opened, closed = (edit[2] for edit in sender.text_edits)
    assert opened.startswith(f"🚨 СРОЧНО · 🏃 Заявка #{request.daily_number} — в работе")
    assert closed.startswith(f"✅ Заявка #{request.daily_number} — выполнена")


@pytest.mark.parametrize(
    "sent_text",
    ["🔔 Новая заявка #1", "🔔 Новая заявка #1\nСуть: без строки хода", "одна строка\n\nХод: …"],
)
def test_text_not_shaped_like_card_has_no_body(sent_text: str) -> None:
    """Не карточка (старое сообщение, обрезанный текст) — меняются только кнопки."""
    assert staff_card.body_of(sent_text) is None

"""«↩️ Вернуть в работу» в чате службы (spec 0080 §3, issue #55).

Под выполненной заявкой — кнопка возврата; нажатие возвращает её в работу и
перерисовывает кнопки под `in_progress`. У заявки формы кнопки нет, отказ домена —
тостом, без строки в группу (S-2). Без HTTP: `handle_staff_message` напрямую.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"
Status = requests_api.RequestStatus


@pytest.fixture(autouse=True)
def _staff_chat_is_default() -> Iterator[None]:
    """Дефолтный чат ведёт заявки всех служб (spec 0074 §4) — канон `test_staff.py`."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _press(request_id: uuid.UUID, action: keyboards.CallbackAction) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=f"callback:{uuid.uuid4()}",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.CALLBACK,
        text=keyboards.build_callback_data(request_id, action),
        reply_to=ReplyTo(external_message_id="n1", text="🔔 Новая заявка #1"),
        callback_id=f"cb-{uuid.uuid4()}",
        actor_external_id="42",
    )


async def _taken(tenant_id: uuid.UUID, origin: requests_api.ServiceRequestOrigin) -> uuid.UUID:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id, origin=origin, summary="убрать 305"
            )
        )
        await requests_api.change_request_status(request.id, Status.IN_PROGRESS)
    return request.id


async def _run(tenant_id: uuid.UUID, message: NormalizedMessage) -> RecordingSender:
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def test_done_gets_reopen_button_and_pressing_it_reopens(demo_tenant: uuid.UUID) -> None:
    request_id = await _taken(demo_tenant, requests_api.ServiceRequestOrigin.GUEST_CHAT)

    done = await _run(demo_tenant, _press(request_id, keyboards.CallbackAction.DONE))
    ((_, _, done_markup),) = done.keyboard_edits
    assert done_markup is not None and "Вернуть в работу" in str(done_markup)

    reopened = await _run(demo_tenant, _press(request_id, keyboards.CallbackAction.REOPEN))
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(request_id)).status is Status.IN_PROGRESS
    assert len(reopened.sent) == 1 and "в работе" in reopened.sent[0][1]
    ((_, _, markup),) = reopened.keyboard_edits
    assert markup is not None and "Готово" in str(markup)

    again = await _run(demo_tenant, _press(request_id, keyboards.CallbackAction.REOPEN))
    assert again.sent == []
    assert len(again.toasts) == 1 and "уже в другом состоянии" in again.toasts[0][1]


async def test_showroom_request_has_no_reopen_button(demo_tenant: uuid.UUID) -> None:
    """Запрос с витрины не возвращается; заявки форм гостя — возвращаются (#998)."""
    request_id = await _taken(demo_tenant, requests_api.ServiceRequestOrigin.SHOWROOM_FORM)

    done = await _run(demo_tenant, _press(request_id, keyboards.CallbackAction.DONE))
    ((_, _, markup),) = done.keyboard_edits
    assert markup is None

    pressed = await _run(demo_tenant, _press(request_id, keyboards.CallbackAction.REOPEN))
    assert pressed.sent == []
    assert len(pressed.toasts) == 1 and "Прошли сутки" in pressed.toasts[0][1]

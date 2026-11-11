"""Поправка гостю после возврата выполненной в работу (spec 0080 §4, issue #55).

Гость прочитал «выполнена» — получает поправку; следующее «Готово» — снова
«выполнена», а не дубль по ключу. «Выполнена», опоздавшее к возврату, не уходит,
и поправлять тогда нечего. Повтор доставки события поправку не дублирует.
"""

from __future__ import annotations

import uuid

from hospitality.channels.common.store import ensure_conversation, record_request_origin
from hospitality.channels.telegram.notifications import notify_guest_on_request_closed
from hospitality.channels.telegram.reopen_notice import notify_guest_on_request_reopened
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

Status = requests_api.RequestStatus
_DONE = requests_api.RequestStatusChanged(
    request_id=uuid.uuid4(), old_status=Status.IN_PROGRESS, new_status=Status.DONE
)


def _done(request_id: uuid.UUID) -> requests_api.RequestStatusChanged:
    return _DONE.model_copy(update={"request_id": request_id})


def _reopened(request_id: uuid.UUID) -> requests_api.RequestStatusChanged:
    return requests_api.RequestStatusChanged(
        request_id=request_id, old_status=Status.DONE, new_status=Status.IN_PROGRESS
    )


async def _chat_request(tenant_id: uuid.UUID) -> uuid.UUID:
    """Заявка из чата гостя, взятая и закрытая «Готово»."""
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="Принести 2 полотенца",
                room_number="807",
            )
        )
        conversation_id = await ensure_conversation("telegram", "559")
        await record_request_origin(request.id, conversation_id)
        await requests_api.change_request_status(request.id, Status.IN_PROGRESS)
        await requests_api.change_request_status(request.id, Status.DONE)
    return request.id


async def test_guest_gets_correction_then_a_fresh_done(demo_tenant: uuid.UUID) -> None:
    request_id = await _chat_request(demo_tenant)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await notify_guest_on_request_closed(_done(request_id), sender=sender)
        await requests_api.reopen_request(request_id)
        await notify_guest_on_request_reopened(_reopened(request_id), sender=sender)
        # Повтор доставки того же события — без второй поправки.
        await notify_guest_on_request_reopened(_reopened(request_id), sender=sender)
        await requests_api.change_request_status(request_id, Status.DONE)
        await notify_guest_on_request_closed(_done(request_id), sender=sender)

    texts = [text for chat_id, text in sender.sent if chat_id == "559"]
    assert len(sender.sent) == 3
    assert "выполнена" in texts[0]
    assert texts[1] == (
        "Простите, мы поторопились: заявка «Принести 2 полотенца» ещё не выполнена. "
        "Мы уже занимаемся ею."
    )
    assert "выполнена. Спасибо" in texts[2]


async def test_done_late_to_the_reopen_is_not_sent_and_needs_no_correction(
    demo_tenant: uuid.UUID,
) -> None:
    """Вернули раньше, чем воркер доставил «выполнена», — гостю ни того, ни другого."""
    request_id = await _chat_request(demo_tenant)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await requests_api.reopen_request(request_id)
        await notify_guest_on_request_closed(_done(request_id), sender=sender)
        await notify_guest_on_request_reopened(_reopened(request_id), sender=sender)

    assert sender.sent == []


async def test_other_transitions_are_not_corrections(demo_tenant: uuid.UUID) -> None:
    request_id = await _chat_request(demo_tenant)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await notify_guest_on_request_closed(_done(request_id), sender=sender)
        await notify_guest_on_request_reopened(
            requests_api.RequestStatusChanged(
                request_id=request_id, old_status=Status.NEW, new_status=Status.IN_PROGRESS
            ),
            sender=sender,
        )

    assert len(sender.sent) == 1

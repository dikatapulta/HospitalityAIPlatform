"""Текст гостю об отмене — по причине (spec 0057 §2).

Причина из списка — свой текст без записки персонала; «Другое» и пустая
причина — прежний шаблон с «От персонала: …»; у дубля — ни сообщения, ни
строки `Message` (повтор доставки не «доотправит» его позже).
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_request_origin,
)
from hospitality.channels.telegram.notifications import notify_guest_on_request_closed
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

Reason = requests_api.CancelReason


async def _cancel(
    tenant_id: uuid.UUID, reason: Reason | None, note: str | None = None
) -> tuple[uuid.UUID, RecordingSender]:
    """Заявка из чата гостя, отменённая сотрудником, и доставка события гостю."""
    sender = RecordingSender()
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
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.CANCELLED,
            resolution_note=note,
            cancel_reason=reason,
            initiator=requests_api.RequestInitiator.STAFF,
        )
        await notify_guest_on_request_closed(
            requests_api.RequestStatusChanged(
                request_id=request.id,
                old_status=requests_api.RequestStatus.NEW,
                new_status=requests_api.RequestStatus.CANCELLED,
                initiator=requests_api.RequestInitiator.STAFF,
            ),
            sender=sender,
        )
    return request.id, sender


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (Reason.UNAVAILABLE, "этого сейчас нет в наличии"),
        (Reason.GUEST_DECLINED, "как мы поняли, она больше не нужна"),
        (Reason.NOT_PROVIDED, "такой услуги у нас нет"),
    ],
)
async def test_reason_from_list_has_own_text(
    demo_tenant: uuid.UUID, reason: Reason, expected: str
) -> None:
    _, sender = await _cancel(demo_tenant, reason)
    [(chat_id, text)] = sender.sent
    assert chat_id == "559"
    assert expected in text
    assert "«Принести 2 полотенца»" in text
    assert "От персонала" not in text
    assert "пришлось отменить" not in text


async def test_duplicate_sends_nothing(demo_tenant: uuid.UUID) -> None:
    request_id, sender = await _cancel(demo_tenant, Reason.DUPLICATE)
    assert sender.sent == []
    with tenant_context(demo_tenant):
        assert not await notification_already_sent(f"guest:request_cancelled:{request_id}")


@pytest.mark.parametrize("reason", [Reason.OTHER, None])
async def test_other_and_empty_reason_keep_staff_note(
    demo_tenant: uuid.UUID, reason: Reason | None
) -> None:
    """«Другое» (и путь без причины — Telegram до PR 2) — прежний шаблон с запиской."""
    _, sender = await _cancel(demo_tenant, reason, note="полотенца в стирке до вечера")
    [(_, text)] = sender.sent
    assert "пришлось отменить" in text
    assert "От персонала: полотенца в стирке до вечера" in text

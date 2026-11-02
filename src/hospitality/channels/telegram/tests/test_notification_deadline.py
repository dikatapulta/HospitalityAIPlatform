"""Потолок вызовов модели в уведомлениях воркера (issue #416, spec 0055 §5).

Провайдер завис: уведомление уходит с деградацией (оригинал сути — службе,
канонический русский текст — гостю) не позже потолка, а не после всех попыток
шлюза (3 × 30 с плюс паузы; у частичного выполнения — вдвое дольше). Пока
уведомление ждало, общий цикл воркера стоял, и за ним — уведомления остальных.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from hospitality.ai.gateway.api import LlmRequest
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.channels.common.store import ensure_conversation, record_request_origin
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.notifications import (
    notify_guest_on_request_closed,
    notify_staff_on_request_created,
)
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

# Тест обязан упасть быстро, а не висеть полторы минуты, если потолок сломан.
GUARD_SECONDS = 10.0


class _HangingProvider:
    """Провайдер, который не отвечает никогда, — «завис» без таймаута SDK."""

    name = "mock"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.call_count += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _shorten_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Не автофикстурой: `monkeypatch` поднялся бы раньше временной БД, и её
    # удаление шло бы под ещё не откаченным POSTGRES_DB («currently open database»).
    assert get_settings().llm_timeout_seconds >= GUARD_SECONDS  # иначе тест ничего не докажет
    monkeypatch.setattr(notifications, "NOTIFICATION_LLM_DEADLINE_SECONDS", 0.2)


async def _make_request(
    tenant_id: uuid.UUID, *, summary: str, guest_language: str
) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number="305",
                guest_language=guest_language,
            )
        )


async def test_staff_notification_goes_out_with_original_summary_at_the_deadline(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shorten_deadline(monkeypatch)
    request = await _make_request(demo_tenant, summary="please clean room 305", guest_language="en")
    provider = _HangingProvider()
    sender = RecordingSender()
    event = requests_api.RequestCreated(
        request_id=request.id, category_id=request.category_id, summary=request.summary
    )
    with tenant_context(demo_tenant):
        async with asyncio.timeout(GUARD_SECONDS):
            await notify_staff_on_request_created(
                event, sender=sender, default_staff_chat_id="999", translate_provider=provider
            )
    ((_, text),) = sender.sent
    assert "please clean room 305" in text  # оригинал вместо перевода (§7.8)
    assert provider.call_count == 1  # повтора после зависшей попытки нет


async def test_partial_completion_shares_one_deadline_with_the_translation(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбор текста о частичном выполнении съел потолок — перевод уже не зовётся,
    гостю уходит честный русский шаблон, а событие не стоит вдвое дольше."""
    _shorten_deadline(monkeypatch)
    request = await _make_request(demo_tenant, summary="убрать 305", guest_language="kk")
    provider = _HangingProvider()
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(request.id, requests_api.RequestStatus.IN_PROGRESS)
        await requests_api.change_request_status(
            request.id, requests_api.RequestStatus.DONE, resolution_note="пылесос сломался"
        )
        conversation_id = await ensure_conversation("telegram", "571")
        await record_request_origin(request.id, conversation_id)
        event = requests_api.RequestStatusChanged(
            request_id=request.id,
            old_status=requests_api.RequestStatus.IN_PROGRESS,
            new_status=requests_api.RequestStatus.DONE,
        )
        async with asyncio.timeout(GUARD_SECONDS):
            await notify_guest_on_request_closed(event, sender=sender, translate_provider=provider)
    ((_, text),) = sender.sent
    assert "выполнена частично" in text
    assert "От персонала: пылесос сломался" in text
    assert provider.call_count == 1  # только сбор текста; перевод потолок не пустил

"""Граница службы для Telegram-чатов персонала (spec 0074 §4, проба О-2 аудита 26.10).

Чат службы ведёт только заявки категорий, размеченных на него; дефолтный чат,
чат сводки и чат ресепшена — любые. Проверяется через `handle_staff_message` —
тот же вход, что у вебхука после развилки «кто персонал».
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.staff_scope import FOREIGN_SERVICE_HINT, chat_category_ids
from hospitality.channels.telegram.tests.conftest import RecordingSender, set_staff_routing
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

DEFAULT_CHAT = "100"
HOUSEKEEPING_CHAT = "101"
MAINTENANCE_CHAT = "102"
RECEPTION_CHAT = "103"
SUMMARY_CHAT = "104"


@pytest.fixture(autouse=True)
def _default_chat() -> Iterator[None]:
    # Свой MonkeyPatch.context(): автофикстура с общим `monkeypatch` ломает откат БД.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TELEGRAM_STAFF_CHAT_ID", DEFAULT_CHAT)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
async def hotel(demo_tenant: uuid.UUID) -> dict[str, uuid.UUID]:
    """Две службы с заявкой #1 у каждой и разметка чатов служб."""
    await set_staff_routing(
        demo_tenant,
        {
            "housekeeping": HOUSEKEEPING_CHAT,
            "maintenance": MAINTENANCE_CHAT,
            "reception": RECEPTION_CHAT,
        },
        daily_summary_chat_id=SUMMARY_CHAT,
    )
    requests: dict[str, uuid.UUID] = {"tenant": demo_tenant}
    with tenant_context(demo_tenant):
        for key in ("housekeeping", "maintenance", "reception"):
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=key)
            )
            if key == "reception":
                continue
            request = await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=category.id,
                    origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                    summary=f"заявка {key}",
                )
            )
            requests[key] = request.id
    return requests


def _message(chat_id: str, text: str, *, kind: MessageKind = MessageKind.TEXT) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=chat_id,
        external_message_id="1",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=kind,
        text=text,
        callback_id="cb-1" if kind is MessageKind.CALLBACK else None,
    )


async def _send(tenant_id: uuid.UUID, message: NormalizedMessage) -> RecordingSender:
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", message.chat_id)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def _status(tenant_id: uuid.UUID, request_id: uuid.UUID) -> requests_api.RequestStatus:
    with tenant_context(tenant_id):
        return (await requests_api.get_request(request_id)).status


async def test_service_chat_cannot_move_foreign_request(hotel: dict[str, uuid.UUID]) -> None:
    """Проба аудита: `/start <id>` инженерной заявки из чата уборки — отказ."""
    tenant_id = hotel["tenant"]
    foreign = hotel["maintenance"]

    sender = await _send(tenant_id, _message(HOUSEKEEPING_CHAT, f"/start {foreign}"))

    assert sender.sent[0][1].endswith(FOREIGN_SERVICE_HINT)
    assert await _status(tenant_id, foreign) is requests_api.RequestStatus.NEW


async def test_daily_number_sees_only_own_service(hotel: dict[str, uuid.UUID]) -> None:
    """`#N` инженерной заявки из чата уборки не находится: чужая служба не видна."""
    tenant_id = hotel["tenant"]
    with tenant_context(tenant_id):
        foreign = await requests_api.get_request(hotel["maintenance"])

    sender = await _send(tenant_id, _message(HOUSEKEEPING_CHAT, f"/start {foreign.daily_number}"))

    assert sender.sent[0][1] == f"Заявка #{foreign.daily_number} среди незакрытых не найдена."
    assert await _status(tenant_id, foreign.id) is requests_api.RequestStatus.NEW
    await _send(tenant_id, _message(MAINTENANCE_CHAT, f"/start {foreign.daily_number}"))
    assert await _status(tenant_id, foreign.id) is requests_api.RequestStatus.IN_PROGRESS


async def test_forged_button_with_foreign_id_is_refused(hotel: dict[str, uuid.UUID]) -> None:
    """Кнопка с чужим UUID (callback_data подделать можно) — тост, статус прежний."""
    tenant_id = hotel["tenant"]
    foreign = hotel["maintenance"]
    data = keyboards.build_callback_data(foreign, keyboards.CallbackAction.CANCEL)

    sender = await _send(tenant_id, _message(HOUSEKEEPING_CHAT, data, kind=MessageKind.CALLBACK))

    assert sender.toasts == [("cb-1", FOREIGN_SERVICE_HINT)]
    assert sender.sent == []
    assert await _status(tenant_id, foreign) is requests_api.RequestStatus.NEW


@pytest.mark.parametrize("chat_id", [DEFAULT_CHAT, RECEPTION_CHAT, SUMMARY_CHAT])
async def test_general_chats_move_any_service(hotel: dict[str, uuid.UUID], chat_id: str) -> None:
    """Дефолтный чат, чат ресепшена и группа руководства — как ресепшен и менеджер в RBAC."""
    tenant_id = hotel["tenant"]
    foreign = hotel["maintenance"]

    await _send(tenant_id, _message(chat_id, f"/start {foreign}"))

    assert await _status(tenant_id, foreign) is requests_api.RequestStatus.IN_PROGRESS


async def test_unreadable_config_closes_service_chat(demo_tenant: uuid.UUID) -> None:
    """Конфига нет — чат службы не может ничего, дефолтный по-прежнему может всё."""
    with tenant_context(demo_tenant):
        assert await chat_category_ids(HOUSEKEEPING_CHAT) == frozenset()
        assert await chat_category_ids(DEFAULT_CHAT) is None

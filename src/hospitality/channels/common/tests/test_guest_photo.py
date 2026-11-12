"""Фото гостя к заявке — правило привязки и шаг хода (spec 0081 §3, §10; issue #82).

Живой Postgres: заявки, связи `request_origins` и `request_photos`, файлы узла (Fake-хранилище).
Исход оркестратора подменяется, как в `test_turn_batch.py`: здесь проверяется, что канал
делает с фото, а не что решает модель.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from structlog.testing import capture_logs

from hospitality.ai import orchestrator
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.channels.common import guest_photo
from hospitality.channels.common.guest_photo import (
    PhotoOutcome,
    attach_pending_photos,
    insert_inbound_photo,
    receive_guest_photo,
    reply_text,
)
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.models import Message
from hospitality.channels.common.store import (
    ensure_conversation,
    load_dialog_history,
    record_request_origin,
    set_pending_action,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import FilePurpose, store_upload
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_file_content import make_png

pytestmark = pytest.mark.usefixtures("fake_file_storage")

PNG = make_png()


@pytest.fixture
def tenant(demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Тенант и Redis лимита чата в памяти (канон `test_turn_batch.py`)."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    return demo_tenant


async def _request(conversation_id: uuid.UUID | None, summary: str = "кран течёт") -> uuid.UUID:
    """Заявка гостя; с диалогом — привязана к нему, как после хода (ADR-011)."""
    categories = await requests_api.list_categories() or [
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="engineering", name="Инженерная служба")
        )
    ]
    created = await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=categories[0].id,
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary=summary,
        )
    )
    if conversation_id is not None:
        await record_request_origin(created.id, conversation_id)
    return created.id


async def _photo(conversation_id: uuid.UUID, *, age: timedelta | None = None) -> uuid.UUID:
    """Входящее фото гостя в диалоге; `age` — сдвинуть его в прошлое."""
    stored = await store_upload(FilePurpose.REQUEST_PHOTO, PNG, "tap.png")
    key = uuid.uuid4()
    message = await insert_inbound_photo(
        conversation_id,
        stored.id,
        idempotency_key=f"web:photo:{key}",
        external_message_id=str(key),
        correlation_id="test",
    )
    assert message is not None
    if age is not None:
        async with session_scope() as session:
            await session.execute(
                update(Message).where(Message.id == message.id).values(created_at=utc_now() - age)
            )
    return stored.id


async def _photos_of(request_id: uuid.UUID) -> list[uuid.UUID]:
    return (await requests_api.list_request_photo_ids([request_id])).get(request_id, [])


async def test_fresh_open_request_gets_the_photo(tenant: uuid.UUID) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        request_id = await _request(conversation_id)
        file_id = await _photo(conversation_id)
        reply = await receive_guest_photo(
            conversation_id, file_id, language="ru", correlation_id="test"
        )
        photos = await _photos_of(request_id)
        number = (await requests_api.get_request(request_id)).daily_number
    assert (reply.outcome, reply.request_id) == (PhotoOutcome.ATTACHED, request_id)
    assert reply.text == f"Фото приложили к заявке #{number} — служба его увидит."
    assert photos == [file_id]


async def test_request_older_than_hour_leaves_photo_waiting_for_text(
    tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        request_id = await _request(conversation_id)
        file_id = await _photo(conversation_id)
        later = utc_now() + timedelta(minutes=61)
        monkeypatch.setattr(guest_photo, "utc_now", lambda: later)
        reply = await receive_guest_photo(
            conversation_id, file_id, language="en", correlation_id="test"
        )
        photos = await _photos_of(request_id)
    assert (reply.outcome, reply.request_id) == (PhotoOutcome.PENDING, None)
    assert reply.text.startswith("Got your photo. Tell us in a few words what happened")
    assert photos == []


async def test_bot_waiting_for_yes_promises_to_attach_after_confirmation(
    tenant: uuid.UUID,
) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        await set_pending_action(
            conversation_id, {"tool_name": "create_service_request", "arguments": {}}
        )
        file_id = await _photo(conversation_id)
        reply = await receive_guest_photo(
            conversation_id, file_id, language="kk", correlation_id="test"
        )
    assert reply.outcome is PhotoOutcome.AWAITING_CONFIRMATION
    assert reply.text == "Фотоны алдық — өтінімді растағаннан кейін оған тіркейміз."


async def test_sixth_photo_stays_in_the_dialog(tenant: uuid.UUID) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        request_id = await _request(conversation_id)
        first = [await _photo(conversation_id) for _ in range(requests_api.MAX_REQUEST_PHOTOS)]
        await requests_api.attach_request_photos(request_id, first)
        sixth = await _photo(conversation_id)
        reply = await receive_guest_photo(
            conversation_id, sixth, language="ru", correlation_id="test"
        )
        photos = await _photos_of(request_id)
    assert reply.outcome is PhotoOutcome.REQUEST_FULL
    assert "уже приложено 5 фото" in reply.text
    assert photos == first


def test_reply_without_daily_number_names_your_request() -> None:
    assert reply_text(PhotoOutcome.ATTACHED, None, "en") == (
        "Photo added to your request — the team will see it."
    )
    assert reply_text(PhotoOutcome.REQUEST_FULL, 7, "kk").startswith("#7 өтінімге 5 фото")


async def _turn(conversation_id: uuid.UUID, replies: list[str]) -> None:
    async def reply(text: str) -> None:
        replies.append(text)

    await run_guest_turn(
        conversation_id,
        "да",
        uuid.uuid4(),
        external_id="guest-1",
        rate_limit_key="stay-1",
        reply=reply,
        provider=None,
        verified_room_number="305",
        verified_stay_id=None,
    )


async def test_turn_gives_waiting_photos_to_every_request_of_the_turn(
    tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies: list[str] = []
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        stale = await _photo(conversation_id, age=timedelta(minutes=61))
        earlier = await _request(None)
        taken = await _photo(conversation_id)
        await requests_api.attach_request_photos(earlier, [taken])
        waiting = [await _photo(conversation_id), await _photo(conversation_id)]
        tap, towels = await _request(None, "кран"), await _request(None, "полотенца")

        async def done(**_: Any) -> OrchestratorTurn:
            return OrchestratorTurn(
                kind=TurnKind.ACTION_DONE, reply_text="Готово.", created_request_ids=(tap, towels)
            )

        monkeypatch.setattr(orchestrator, "handle_message", done)
        await _turn(conversation_id, replies)
        photos = {request_id: await _photos_of(request_id) for request_id in (tap, towels)}
    assert replies == ["Готово."]
    # Обе заявки хода — с обоими ждущими фото, старшие первыми; старое и приложенное — нет.
    assert photos == {tap: waiting, towels: waiting}
    assert stale not in waiting


async def test_failed_attach_step_keeps_the_reply(
    tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies: list[str] = []
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        await _photo(conversation_id)
        request_id = await _request(None)

        async def done(**_: Any) -> OrchestratorTurn:
            return OrchestratorTurn(
                kind=TurnKind.ACTION_DONE, reply_text="Готово.", created_request_ids=(request_id,)
            )

        async def broken(*_: Any) -> None:
            raise RuntimeError("БД мигнула")

        monkeypatch.setattr(orchestrator, "handle_message", done)
        monkeypatch.setattr(requests_api, "attach_request_photos", broken)
        with capture_logs() as logs:
            await _turn(conversation_id, replies)
    assert replies == ["Готово."]
    assert any(
        log["event"] == "guest_photo_attach_failed" and log["log_level"] == "warning"
        for log in logs
    )


async def test_photo_is_not_in_model_history(tenant: uuid.UUID) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "guest-1")
        file_id = await _photo(conversation_id)
        reply = await receive_guest_photo(
            conversation_id, file_id, language="ru", correlation_id="test"
        )
        await attach_pending_photos(conversation_id, [])
        history = await load_dialog_history(conversation_id, exclude_message_id=uuid.uuid4())
    # Модель знает о фото только по ответу отеля — своей реплике (§4).
    assert [text for _, text in history] == [reply.text]

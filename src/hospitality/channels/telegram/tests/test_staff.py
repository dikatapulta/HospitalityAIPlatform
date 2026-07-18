"""Команды персонала в staff-чате (Task 0017, ADR-011).

Проверяет разбор и исполнение команд напрямую (`handle_staff_message`), без HTTP:
успешный переход, недопустимый переход, неизвестная команда, кривой id. Ответ
персоналу перехватывается фейк-отправителем.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        return "m1"


def _command(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id="1",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=text,
    )


async def _make_request(tenant_id: uuid.UUID, key: str = "housekeeping") -> uuid.UUID:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="убрать 305")
        )
    return request.id


async def _run(tenant_id: uuid.UUID, text: str) -> str:
    """Прогнать команду, вернуть текст ответа персоналу."""
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(STAFF_CHAT)
        await handle_staff_message(
            conversation_id, _command(text), sender=sender, correlation_id="c1"
        )
    assert len(sender.sent) == 1
    chat_id, reply = sender.sent[0]
    assert chat_id == STAFF_CHAT
    return reply


async def _run_message(tenant_id: uuid.UUID, message: NormalizedMessage) -> list[tuple[str, str]]:
    """Прогнать произвольное сообщение; вернуть всё, что бот отправил (может быть пусто)."""
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender.sent


async def test_valid_transition_moves_request(demo_tenant: uuid.UUID) -> None:
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/start {request_id}")
    assert "in_progress" in reply
    with tenant_context(demo_tenant):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS


async def test_start_by_daily_number_moves_request(demo_tenant: uuid.UUID) -> None:
    """Команда с дневным номером `/start 1` находит незакрытую заявку и двигает её.

    Первая заявка дня — `#1`; ответ персоналу тоже называет её номером (S-3, #38).
    """
    await _make_request(demo_tenant)  # первая за день → #1, статус NEW
    reply = await _run(demo_tenant, "/start 1")
    assert "in_progress" in reply
    assert "#1" in reply


async def test_daily_number_accepts_hash_prefix(demo_tenant: uuid.UUID) -> None:
    """`/start #1` — ведущий `#` в номере допускается (как в уведомлении)."""
    await _make_request(demo_tenant)
    reply = await _run(demo_tenant, "/start #1")
    assert "in_progress" in reply


async def test_assign_replies_with_retirement_hint(demo_tenant: uuid.UUID) -> None:
    """`/assign` упразднён (ADR-013): бот переучивает подсказкой, а не молчит.

    Персонал недели пользовался старой схемой — тишина выглядела бы поломкой;
    заявка при этом не двигается.
    """
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/assign {request_id}")
    assert "/start" in reply
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(request_id)).status is requests_api.RequestStatus.NEW


async def test_unknown_daily_number_reports_not_found(demo_tenant: uuid.UUID) -> None:
    """Номер, которого нет среди незакрытых → понятное «не найдена», не UUID-ошибка."""
    await _make_request(demo_tenant)
    reply = await _run(demo_tenant, "/start 42")
    assert "#42" in reply
    assert "не найдена" in reply


async def test_ambiguous_daily_number_asks_to_clarify(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один `#N` у нескольких незакрытых (номер за сутки повторился) → просим уточнить.

    Номер — метка, не ключ (issue #38): staff.py не гадает, а перечисляет
    кандидатов с полными id для однозначной команды.
    """
    now = datetime.now(UTC)
    candidates = [
        requests_api.ServiceRequestRead(
            id=uuid.uuid4(),
            category_id=uuid.uuid4(),
            status=requests_api.RequestStatus.NEW,
            summary=summary,
            details=None,
            room_number=room,
            daily_number=7,
            guest_language=None,
            created_at=now,
            updated_at=now,
        )
        for summary, room in [("полотенца", "305"), ("лампочка", "210")]
    ]

    async def fake_find(daily_number: int) -> list[requests_api.ServiceRequestRead]:
        assert daily_number == 7
        return candidates

    monkeypatch.setattr(requests_api, "find_open_requests_by_daily_number", fake_find)

    reply = await _run(demo_tenant, "/done 7")
    assert "уточните" in reply.lower()
    for candidate in candidates:
        assert str(candidate.id) in reply  # полный id каждого кандидата — для команды


async def test_command_with_bot_suffix_is_accepted(demo_tenant: uuid.UUID) -> None:
    # В группах Telegram дописывает @botusername к команде — он не должен мешать.
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/start@demo_bot {request_id}")
    assert "in_progress" in reply


async def test_invalid_transition_reports_error_and_keeps_status(demo_tenant: uuid.UUID) -> None:
    request_id = await _make_request(demo_tenant)  # заявка NEW: new → done запрещён
    reply = await _run(demo_tenant, f"/done {request_id}")
    assert requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION in reply
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(request_id)).status is requests_api.RequestStatus.NEW


async def test_unknown_request_reports_not_found(demo_tenant: uuid.UUID) -> None:
    reply = await _run(demo_tenant, f"/start {uuid.uuid4()}")
    assert requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND in reply


@pytest.mark.parametrize(
    "text",
    ["/frobnicate 123", "/done", "/done не-uuid"],
)
async def test_bad_command_returns_hint_not_crash(demo_tenant: uuid.UUID, text: str) -> None:
    # Попытка команды (текст с "/") заслуживает подсказки, а не тишины.
    reply = await _run(demo_tenant, text)
    assert reply  # понятная подсказка, а не исключение


@pytest.mark.parametrize(
    "text",
    ["привет", "Аня, зайди на 305", "спасибо, всё сделали", "ok"],
)
async def test_non_command_is_silent(demo_tenant: uuid.UUID, text: str) -> None:
    """Обычная реплика в staff-группе (без ведущего "/") → бот молчит (S-2, #38 п.4).

    Иначе бот отвечает подсказкой на каждое сообщение живой группы — её мьютят, и
    вместе со спамом теряются уведомления о заявках.
    """
    assert await _run_message(demo_tenant, _command(text)) == []


async def test_non_text_message_is_silent(demo_tenant: uuid.UUID) -> None:
    """Фото/стикер/голос в staff-группе (UNSUPPORTED) → бот молчит, не шлёт подсказку."""
    message = NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id="1",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.UNSUPPORTED,
        text=None,
    )
    assert await _run_message(demo_tenant, message) == []

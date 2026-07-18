"""Команды персонала в staff-чате (Task 0017, ADR-011).

Проверяет разбор и исполнение команд напрямую (`handle_staff_message`), без HTTP:
успешный переход, недопустимый переход, неизвестная команда, кривой id. Ответ
персоналу перехватывается фейк-отправителем.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.store import ensure_conversation, record_outbound_message
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное/кнопки/тосты."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict[str, Any] | None] = []
        self.toasts: list[tuple[str, str]] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return "m" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.toasts.append((callback_id, text))

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


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
            resolution_note=None,
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


# ---------------------------------------------------------------------------
# Inline-кнопки, команды-реплаи и примечание закрытия (spec 0021 П-2/П-4, #38)


def _callback(
    data: str, *, reply_message_id: str = "n1", reply_text: str = "🔔 Новая заявка #1"
) -> NormalizedMessage:
    """Нажатие кнопки под сообщением бота (как отдаёт normalize_update)."""
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=f"callback:{uuid.uuid4()}",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.CALLBACK,
        text=data,
        reply_to=ReplyTo(external_message_id=reply_message_id, text=reply_text),
        callback_id=f"cb-{uuid.uuid4()}",
        actor_external_id="42",
    )


def _reply_text_message(text: str, *, reply_message_id: str, reply_text: str) -> NormalizedMessage:
    """Текст ответом (reply) на сообщение бота."""
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=str(uuid.uuid4()),
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=text,
        reply_to=ReplyTo(external_message_id=reply_message_id, text=reply_text),
        actor_external_id="42",
    )


async def _run_with_sender(
    tenant_id: uuid.UUID, message: NormalizedMessage, sender: RecordingSender | None = None
) -> RecordingSender:
    """Прогнать сообщение, вернуть отправитель целиком (тосты/кнопки/сообщения)."""
    sender = sender or RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def _seed_notification(tenant_id: uuid.UUID, request_id: uuid.UUID) -> str:
    """Записать «уведомление о заявке» как это делает notifications.py; вернуть его msg id."""
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(STAFF_CHAT)
        await record_outbound_message(
            conversation_id,
            "🔔 Новая заявка #1",
            "c0",
            external_message_id="n1",
            idempotency_key=f"staff:request_created:{request_id}",
        )
    return "n1"


async def test_callback_start_moves_request_and_updates_keyboard(
    demo_tenant: uuid.UUID,
) -> None:
    """Кнопка «Взять в работу»: переход по той же карте (P-5), тост нажавшему,
    строка-итог в чат и перерисовка клавиатуры под новый статус (#38 п.2)."""
    request_id = await _make_request(demo_tenant)
    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.START)
    sender = await _run_with_sender(demo_tenant, _callback(data))

    with tenant_context(demo_tenant):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS
    assert len(sender.toasts) == 1 and "in_progress" in sender.toasts[0][1]
    assert len(sender.sent) == 1 and "in_progress" in sender.sent[0][1]
    # Клавиатура уведомления перерисована под in_progress: есть «Готово».
    ((_, message_id, markup),) = sender.keyboard_edits
    assert message_id == "n1"
    assert markup is not None and "Готово" in str(markup)


async def test_callback_stale_button_toasts_without_chat_spam(demo_tenant: uuid.UUID) -> None:
    """Второе нажатие той же кнопки: недопустимый переход → ТОЛЬКО тост,
    в группу ничего не уходит (S-2: устаревшая кнопка не спамит чат)."""
    request_id = await _make_request(demo_tenant)
    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.START)
    await _run_with_sender(demo_tenant, _callback(data))
    sender = await _run_with_sender(demo_tenant, _callback(data))  # повторное нажатие
    assert len(sender.toasts) == 1
    assert "уже в другом состоянии" in sender.toasts[0][1]
    assert sender.sent == []


async def test_callback_unknown_payload_toasts_help(demo_tenant: uuid.UUID) -> None:
    """Кнопка с чужим/старым payload → вежливый тост, никаких переходов."""
    sender = await _run_with_sender(demo_tenant, _callback("who:knows:what"))
    assert len(sender.toasts) == 1
    assert sender.sent == []


async def test_done_command_with_note_saves_resolution_note(demo_tenant: uuid.UUID) -> None:
    """`/done N текст…` — хвост становится примечанием закрытия и виден в ответе
    (spec 0021 П-4): «что не сделано и почему» уходит гостю уведомлением."""
    request_id = await _make_request(demo_tenant)
    await _run(demo_tenant, f"/start {request_id}")
    reply = await _run(demo_tenant, f"/done {request_id} кофе не принесли — закончился")
    assert "done" in reply
    assert "кофе не принесли — закончился" in reply
    with tenant_context(demo_tenant):
        updated = await requests_api.get_request(request_id)
    assert updated.resolution_note == "кофе не принесли — закончился"


async def test_command_as_reply_to_notification_resolves_request(
    demo_tenant: uuid.UUID,
) -> None:
    """`/start` ответом на уведомление — заявка резолвится по сообщению, номер
    не нужен (#38 п.3): обратный поиск по ключу `staff:request_created:…`."""
    request_id = await _make_request(demo_tenant)
    message_id = await _seed_notification(demo_tenant, request_id)
    message = _reply_text_message(
        "/start", reply_message_id=message_id, reply_text="🔔 Новая заявка #1"
    )
    sender = await _run_with_sender(demo_tenant, message)
    assert len(sender.sent) == 1 and "in_progress" in sender.sent[0][1]
    with tenant_context(demo_tenant):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS


async def test_done_reply_with_note_tail(demo_tenant: uuid.UUID) -> None:
    """`/done кофе нет` ответом на уведомление: весь хвост — примечание."""
    request_id = await _make_request(demo_tenant)
    message_id = await _seed_notification(demo_tenant, request_id)
    await _run(demo_tenant, f"/start {request_id}")
    message = _reply_text_message(
        "/done кофе нет", reply_message_id=message_id, reply_text="🔔 Новая заявка #1"
    )
    await _run_with_sender(demo_tenant, message)
    with tenant_context(demo_tenant):
        updated = await requests_api.get_request(request_id)
    assert updated.status is requests_api.RequestStatus.DONE
    assert updated.resolution_note == "кофе нет"


async def test_done_note_button_then_reply_completes_with_note(
    demo_tenant: uuid.UUID,
) -> None:
    """«⚠️ Готово частично»: бот задаёт вопрос (ForceReply), ответ-реплай персонала
    закрывает заявку с примечанием (spec 0021 П-4) — ноль ручных id."""
    request_id = await _make_request(demo_tenant)
    await _run(demo_tenant, f"/start {request_id}")

    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.DONE_NOTE)
    sender = await _run_with_sender(demo_tenant, _callback(data))
    # Бот спросил «что не сделано?» с ForceReply и ответил тостом «жду примечание».
    assert len(sender.sent) == 1
    question_chat, question_text = sender.sent[0]
    assert "Ответьте на это сообщение" in question_text
    assert sender.markups[0] == {"force_reply": True}
    assert len(sender.toasts) == 1
    question_message_id = "m1"  # первый send фейка

    reply = _reply_text_message(
        "кофе закончился, принесём утром",
        reply_message_id=question_message_id,
        reply_text=question_text,
    )
    sender2 = await _run_with_sender(demo_tenant, reply)
    assert len(sender2.sent) == 1 and "done" in sender2.sent[0][1]
    with tenant_context(demo_tenant):
        updated = await requests_api.get_request(request_id)
    assert updated.status is requests_api.RequestStatus.DONE
    assert updated.resolution_note == "кофе закончился, принесём утром"


async def test_plain_text_reply_to_notification_is_silent(demo_tenant: uuid.UUID) -> None:
    """Обычный текст ответом на УВЕДОМЛЕНИЕ (не на вопрос о примечании) — молчание:
    намерение неочевидно, для переходов есть кнопки и команды (S-2)."""
    request_id = await _make_request(demo_tenant)
    message_id = await _seed_notification(demo_tenant, request_id)
    message = _reply_text_message(
        "посмотрю после обеда", reply_message_id=message_id, reply_text="🔔 Новая заявка #1"
    )
    sender = await _run_with_sender(demo_tenant, message)
    assert sender.sent == []
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(request_id)).status is requests_api.RequestStatus.NEW

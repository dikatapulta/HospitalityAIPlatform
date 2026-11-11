"""Кнопки-кандидаты неоднозначного `#N` в staff-чате (spec 0021 П-2 п.5, #83).

Незакрытая вчерашняя заявка делит `#N` с сегодняшней: команда отвечает списком
с кнопкой на каждого кандидата, тап исполняет ту же команду — с её примечанием —
над выбранной заявкой. Без HTTP: `handle_staff_message` напрямую, Bot API — фейк.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select, text

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.models import Message
from hospitality.channels.common.store import ensure_conversation, record_outbound_message
from hospitality.channels.telegram import keyboards, staff_pick
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"
Status = requests_api.RequestStatus


@pytest.fixture(autouse=True)
def _staff_chat_is_default() -> Iterator[None]:
    """Команды идут из дефолтного чата — он ведёт заявки всех служб (spec 0074 §4).

    Свой `MonkeyPatch.context()`, а не общий `monkeypatch`: автофикстура с ним
    ломает откат БД-фикстур.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


class RecordingSender:
    """Фейк порта TelegramSender: сообщения, кнопки, тосты и перерисовки."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict[str, Any] | None] = []
        self.toasts: list[str] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []
        self.text_edits: list[tuple[str, str, str, dict[str, Any] | None]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return f"m-{uuid.uuid4()}"

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.toasts.append(text)

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))

    async def edit_message_text(
        self, chat_id: str, message_id: str, text: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.text_edits.append((chat_id, message_id, text, reply_markup))


async def _two_with_number_one(
    tenant_id: uuid.UUID,
) -> tuple[requests_api.ServiceRequestRead, requests_api.ServiceRequestRead]:
    """Вчерашняя незакрытая #1 (комн. 505) и сегодняшняя #1 (комн. 212).

    «Вчера» — сдвиг `service_day` первой заявки на день назад: сегодняшний
    счётчик снова стартует с #1 (как `test_daily_number.py`).
    """
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )

        async def create(summary: str, room: str) -> requests_api.ServiceRequestRead:
            return await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=category.id,
                    origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                    summary=summary,
                    room_number=room,
                )
            )

        yesterday = await create("Полотенце", "505")
        async with session_scope() as session:
            await session.execute(
                text("UPDATE service_requests SET service_day = service_day - 1 WHERE id = :id"),
                {"id": yesterday.id},
            )
        today = await create("Утюг", "212")
        yesterday = await requests_api.get_request(yesterday.id)
    assert yesterday.daily_number == today.daily_number == 1
    return yesterday, today


def _command(body: str) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=str(uuid.uuid4()),
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=body,
        actor_external_id="42",
    )


def _press(request_id: uuid.UUID, message_id: str) -> NormalizedMessage:
    """Нажатие кнопки кандидата под ответом `message_id`."""
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=f"callback:{uuid.uuid4()}",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.CALLBACK,
        text=keyboards.build_callback_data(request_id, keyboards.CallbackAction.PICK),
        reply_to=ReplyTo(external_message_id=message_id, text="#1 — таких…"),
        callback_id=f"cb-{uuid.uuid4()}",
        actor_external_id="42",
    )


async def _run(tenant_id: uuid.UUID, message: NormalizedMessage) -> RecordingSender:
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def _offer(tenant_id: uuid.UUID, body: str) -> tuple[RecordingSender, str]:
    """Отправить команду с неоднозначным `#1`; вернуть отправитель и id ответа-списка."""
    sender = await _run(tenant_id, _command(body))
    assert len(sender.sent) == 1
    with tenant_context(tenant_id):
        async with session_scope() as session:
            message_id = await session.scalar(
                select(Message.external_message_id).where(
                    Message.idempotency_key.like("staff:pick:%")
                )
            )
    assert message_id is not None
    return sender, message_id


async def _status(tenant_id: uuid.UUID, request_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        return await requests_api.get_request(request_id)


async def test_ambiguous_number_offers_candidate_buttons(demo_tenant: uuid.UUID) -> None:
    """Список с днём и комнатой и по кнопке на кандидата — без просьбы набрать id."""
    yesterday, today = await _two_with_number_one(demo_tenant)
    sender, _ = await _offer(demo_tenant, "/done 1")

    (_, reply), (markup,) = sender.sent[0], sender.markups
    assert reply.startswith("#1 — таких открытых заявок несколько. Какую закрыть?")
    assert str(yesterday.id) not in reply and str(today.id) not in reply
    assert yesterday.service_day is not None and today.service_day is not None
    assert f"• #1 от {yesterday.service_day:%d.%m} · комн. 505 · «Полотенце»" in reply
    assert f"• #1 от {today.service_day:%d.%m} · комн. 212 · «Утюг»" in reply
    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    # Новые сверху — порядок `find_open_requests_by_daily_number`.
    assert [button["text"] for button in buttons] == [
        f"#1 от {today.service_day:%d.%m} · комн. 212",
        f"#1 от {yesterday.service_day:%d.%m} · комн. 505",
    ]
    assert [keyboards.parse_callback_data(button["callback_data"]) for button in buttons] == [
        keyboards.CallbackData(today.id, keyboards.CallbackAction.PICK),
        keyboards.CallbackData(yesterday.id, keyboards.CallbackAction.PICK),
    ]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


async def test_pick_runs_the_command_on_chosen_request(demo_tenant: uuid.UUID) -> None:
    """Тап — `/start` над выбранной: другая заявка не тронута, кнопки списка сняты,
    кнопки уведомления выбранной перерисованы (а не кнопки под списком)."""
    yesterday, today = await _two_with_number_one(demo_tenant)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await record_outbound_message(
            conversation_id,
            "🔔 Новая заявка #1",
            "c0",
            external_message_id="n-yesterday",
            idempotency_key=f"staff:request_created:{yesterday.id}",
        )
    _, pick_message = await _offer(demo_tenant, "/start 1")

    sender = await _run(demo_tenant, _press(yesterday.id, pick_message))

    assert (await _status(demo_tenant, yesterday.id)).status is Status.IN_PROGRESS
    assert (await _status(demo_tenant, today.id)).status is Status.NEW
    assert sender.toasts == ["Заявка #1 → in_progress."]
    assert len(sender.sent) == 1 and "«Полотенце» → in_progress" in sender.sent[0][1]
    edits = {message_id: markup for _, message_id, markup in sender.keyboard_edits}
    assert edits[pick_message] is None
    assert edits["n-yesterday"] is not None and "Готово" in str(edits["n-yesterday"])


async def test_pick_keeps_the_note_of_the_command(demo_tenant: uuid.UUID) -> None:
    """`/done 1 <текст>`: примечание едет в ответе-списке и закрывает выбранную с ним."""
    yesterday, today = await _two_with_number_one(demo_tenant)
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(today.id, Status.IN_PROGRESS)
    offer, pick_message = await _offer(demo_tenant, "/done 1 кофе не принесли")
    assert offer.sent[0][1].endswith("\nПримечание: кофе не принесли")

    await _run(demo_tenant, _press(today.id, pick_message))

    closed = await _status(demo_tenant, today.id)
    assert closed.status is Status.DONE and closed.resolution_note == "кофе не принесли"
    assert (await _status(demo_tenant, yesterday.id)).status is Status.NEW


async def test_pick_cancel_with_reason(demo_tenant: uuid.UUID) -> None:
    """`/cancel 1 <причина>`: тап отменяет выбранную с причиной «Другое»."""
    yesterday, _ = await _two_with_number_one(demo_tenant)
    offer, pick_message = await _offer(demo_tenant, "/cancel 1 гость передумал")
    assert "Какую отменить?" in offer.sent[0][1]
    assert offer.sent[0][1].endswith("\nПричина: гость передумал")

    await _run(demo_tenant, _press(yesterday.id, pick_message))

    cancelled = await _status(demo_tenant, yesterday.id)
    assert cancelled.status is Status.CANCELLED
    assert cancelled.cancel_reason is requests_api.CancelReason.OTHER
    assert cancelled.resolution_note == "гость передумал"


async def test_pick_cancel_without_reason_opens_reason_list(demo_tenant: uuid.UUID) -> None:
    """`/cancel 1` без причины: тап открывает список причин выбранной под тем же ответом."""
    yesterday, _ = await _two_with_number_one(demo_tenant)
    _, pick_message = await _offer(demo_tenant, "/cancel 1")

    sender = await _run(demo_tenant, _press(yesterday.id, pick_message))

    assert (await _status(demo_tenant, yesterday.id)).status is Status.NEW
    ((_, message_id, markup),) = sender.keyboard_edits
    assert message_id == pick_message
    assert markup == keyboards.cancel_reasons_keyboard(yesterday.id)
    assert sender.sent == []


async def test_pick_under_unknown_message_is_stale(demo_tenant: uuid.UUID) -> None:
    """Записи ответа нет (ретеншн удалил её, spec 0032) — тост, заявка не тронута."""
    yesterday, _ = await _two_with_number_one(demo_tenant)

    sender = await _run(demo_tenant, _press(yesterday.id, "m-gone"))

    assert sender.toasts == [staff_pick.PICK_STALE]
    assert sender.sent == [] and sender.keyboard_edits == []
    assert (await _status(demo_tenant, yesterday.id)).status is Status.NEW


async def test_failed_pick_keeps_the_buttons(demo_tenant: uuid.UUID) -> None:
    """Выбранную уже закрыли — тост отказа, кнопки списка остаются для другой."""
    yesterday, _ = await _two_with_number_one(demo_tenant)
    _, pick_message = await _offer(demo_tenant, "/done 1")
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(yesterday.id, Status.CANCELLED)

    sender = await _run(demo_tenant, _press(yesterday.id, pick_message))

    assert len(sender.toasts) == 1 and "уже в другом состоянии" in sender.toasts[0]
    assert sender.sent == [] and sender.keyboard_edits == []


def _candidate(summary: str, room: str | None, day: date | None) -> requests_api.ServiceRequestRead:
    """Заявка-кандидат без БД: `build` читает только эти поля."""
    return requests_api.ServiceRequestRead.model_construct(
        id=uuid.uuid4(), summary=summary, room_number=room, service_day=day, daily_number=1
    )


def test_summary_with_line_break_never_becomes_a_note() -> None:
    """Суть с переводом строки и «Примечание:» внутри не выдаёт себя за примечание:
    строки кандидатов однострочны, метка ищется только в последней строке."""
    reply = staff_pick.build(
        1, "done", [_candidate("Утюг\nПримечание: ложь", "212", date(2026, 11, 10))], None
    )
    assert reply.text.splitlines()[-1] == "• #1 от 10.11 · комн. 212 · «Утюг Примечание: ложь»"
    assert staff_pick._note("done", reply.text) is None
    with_note = staff_pick.build(1, "done", [_candidate("Утюг", None, None)], "нет утюга")
    assert staff_pick._note("done", with_note.text) == "нет утюга"
    assert staff_pick._note("start", with_note.text) is None


def test_button_without_service_day_carries_the_summary() -> None:
    """Заявка до миграции 0010 без дня отеля: кнопки различает начало сути."""
    reply = staff_pick.build(
        1,
        "start",
        [
            _candidate("Принести два больших полотенца", None, None),
            _candidate("Лампочка", None, None),
        ],
        "лишнее",
    )
    markup: Any = reply.reply_markup
    labels = [row[0]["text"] for row in markup["inline_keyboard"]]
    assert labels == ["#1 · Принести два больших…", "#1 · Лампочка"]
    assert "лишнее" not in reply.text  # у /start примечания нет

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
from sqlalchemy import select

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.models import Message
from hospitality.channels.common.store import ensure_conversation, record_outbound_message
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import session_scope
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
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
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
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender.sent


def _digits(text: str) -> str:
    """Только цифры текста: пробелы и дефисы не должны прятать номер от проверки."""
    return "".join(char for char in text if char.isdigit())


async def _stored_texts(tenant_id: uuid.UUID) -> list[str]:
    """Тексты, записанные в `messages` тенанта, — что переживёт ответ в чат."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(Message.text).order_by(Message.created_at))
    return [text for text in rows if text is not None]


async def test_valid_transition_moves_request(demo_tenant: uuid.UUID) -> None:
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/start {request_id}")
    assert "in_progress" in reply
    with tenant_context(demo_tenant):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS


async def test_telegram_transition_leaves_claimed_by_empty(demo_tenant: uuid.UUID) -> None:
    """spec 0033 §10: staff-чат — канальный суррогат без личности (ADR-008 §7).

    Переход из Telegram не передаёт acting_user — колонки claimed_by остаются
    пустыми, кабинет покажет заявку «в работе» без «взял: Имя».
    """
    request_id = await _make_request(demo_tenant)
    await _run(demo_tenant, f"/start {request_id}")
    with tenant_context(demo_tenant):
        stored = await requests_api.get_request(request_id)
    assert stored.status is requests_api.RequestStatus.IN_PROGRESS
    assert stored.claimed_by_user_id is None
    assert stored.claimed_by_display_name is None


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
            is_urgent=False,
            resolution_note=None,
            claimed_by_user_id=None,
            claimed_by_display_name=None,
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


async def test_luhn_valid_uuid_survives_card_masking(demo_tenant: uuid.UUID) -> None:
    """Регрессия #172: uuid, попавший под шаблон карты, доходит до разбора целым.

    Этот uuid маскируется в `293b[card ****6182]bc-…` (первые 13 цифр проходят
    Луна) — раньше команда разбиралась из маскированного текста, и сотрудник
    получал «Не разобрал» на верную команду примерно раз на 500. Признак
    починки — ответ про НЕНАЙДЕННУЮ заявку: id дошёл до поиска, а не до отказа
    разбора. Такие uuid — 0,2 % (замер на 200 000), отсюда же краснота CI.
    """
    reply = await _run(demo_tenant, "/start 293b1367-2553-4161-82bc-556830aaf725")
    assert "Не разобрал" not in reply
    assert requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND in reply


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("/done 4111-1111-1111-1111", "4111111111111111"),
        ("/done 4111-1111-1111 1111", "411111111111"),
        ("/done 4111-1111 1111-1111", "41111111"),
        ("/done 41111111 11111111", "41111111"),
        ("/done #41111111 11111111", "41111111"),
    ],
)
async def test_card_argument_is_not_quoted_back(
    demo_tenant: uuid.UUID, text: str, leak: str
) -> None:
    """Аргумент-карта не возвращается персоналу ни целым, ни куском (PR #202, NG-3).

    Карта через дефисы — не цифры и не uuid, поэтому доходит ровно до ответа «Не
    разобрал» (второй проход ревью). Формы со **смешанными** разделителями —
    третий проход: разбор идёт по сырому тексту (#172) и рвёт строку по пробелу
    раньше канона, поэтому в цитату попадал кусок PAN — 12 цифр из 16, больше
    отраслевого усечения (первые 6 + последние 4). Канон кусок не прикрывает:
    кандидат в PAN начинается с 13 цифр. Уходило это и в staff-чат, и строкой в
    `messages` на 90 дней. Признак починки — цитаты нет вовсе: аргумент не
    пережил канон в `normalized.text`, значит, цитировать нечего.

    Две последние формы — четвёртый проход: карта с пробелом не по границе
    четвёрок даёт кусок из одних цифр, и наружу он уходил не цитатой, а **ответом
    поиска** — «Заявка #41111111 среди незакрытых не найдена». Тот же сток, вход
    другой: щит цитаты сюда не достаёт, номер заявки отсекает потолок
    `_MAX_DAILY_NUMBER_DIGITS`.
    """
    reply = await _run(demo_tenant, text)
    assert "Не разобрал команду" in reply
    assert leak not in _digits(reply)
    assert await _stored_texts(demo_tenant) == [reply]  # в БД лежит тот же текст


async def test_unparsed_argument_is_masked_in_reply_and_storage(demo_tenant: uuid.UUID) -> None:
    """Эхо ошибки не выносит сырой PAN наружу (ревью PR #202, NG-3, spec 0031 §2).

    Команда разбирается из сырого текста (#172) — значит, аргумент нельзя
    цитировать персоналу как есть: `send_reply` шлёт ответ в группу И пишет его
    строкой в `messages`, где она лежит 90 дней ретеншна.

    Форма взята та, где проверки «пережил канон» НЕ хватает и работает именно
    маскирование цитаты: в `/done 4111111111111111 1` жадный ряд из 17 цифр не
    проходит Луна, движок регулярки не откатывается на 16, и канон проносит PAN
    через общий текст целым. Цитата всё равно маскируется — здесь, отдельно.
    """
    reply = await _run(demo_tenant, "/done 4111111111111111 1")
    assert "Не разобрал «[card ****1111]»" in reply
    assert "4111111111111111" not in _digits(reply)
    assert await _stored_texts(demo_tenant) == [reply]


async def test_typo_argument_is_still_quoted(demo_tenant: uuid.UUID) -> None:
    """Щит цитаты не глушит диагностику: опечатку персоналу по-прежнему показывают.

    Без этой проверки фикс, который выбросил бы цитату всегда, выглядел бы
    зелёным — а сотрудник перестал бы видеть, что именно бот прочитал.
    """
    assert "Не разобрал «12з»" in await _run(demo_tenant, "/done 12з")


@pytest.mark.parametrize(
    "text",
    [
        "/done 4111111111111111",  # слитный PAN — 16 цифр
        "/done 411111111111 1111",  # кусок PAN — 12 цифр, до PR давал DataError
        "/done 77012345678",  # телефон (issue #203)
        "/done ²",  # isdigit(), но не isdecimal() — падал ValueError (issue #203)
    ],
)
async def test_long_numeric_argument_never_reaches_number_lookup(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Длинный числовой аргумент не попадает в `int()` и в запрос (PR #202, #203).

    Вторая ветка аргумента — числовая. Разбор из сырого текста (#172) уводил в
    поиск по дневному номеру всё, что набрано цифрами, а колонка `daily_number`
    — `Integer()`: 10+ цифр драйвер отбивает исключением, чей текст несёт число
    целиком, и уносит его в JSON-лог `unhandled_error` и в Sentry (локалы
    фреймов сняты, текст исключения — нет); вебхук отдаёт 500, а повтор от
    Telegram глушит дедупликация — команда исчезает молча. Надстрочная «²»
    роняет тот же путь строкой раньше: `isdigit()` её принимает, `int()` — нет.

    Щит — потолок дневного номера, поэтому проверка идёт классом входов, а не
    формой карты: до поиска число не доходит ни в одном, и персонал получает
    текст, а не исключение (`_run` требует ровно один ответ — падение обработчика
    провалило бы тест раньше ассертов).
    """
    looked_up: list[int] = []
    original = requests_api.find_open_requests_by_daily_number

    async def _spy(number: int) -> list[requests_api.ServiceRequestRead]:
        looked_up.append(number)
        return await original(number)

    monkeypatch.setattr(requests_api, "find_open_requests_by_daily_number", _spy)

    reply = await _run(demo_tenant, text)

    assert looked_up == []
    assert "Не разобрал" in reply
    assert await _stored_texts(demo_tenant) == [reply]


async def test_daily_number_still_resolves_after_card_guard(demo_tenant: uuid.UUID) -> None:
    """Щит не задевает обычный номер: `#N` в пределах потолка идёт в поиск как прежде."""
    await _make_request(demo_tenant)  # первая за день → #1
    assert "in_progress" in await _run(demo_tenant, "/start #1")


async def test_daily_number_ceiling_is_the_boundary(demo_tenant: uuid.UUID) -> None:
    """Граница щита — ровно потолок: 5 цифр ещё номер, 6 уже нет.

    Номер живёт в пределах суток одного отеля (пилот — 310 номеров), поэтому
    потолок и есть признак: он закрывает класс входов целиком, а не очередную
    форму карты. Проверяются обе стороны границы — иначе потолок, задранный до
    бесполезности, выглядел бы зелёным.
    """
    assert "Заявка #99999 среди незакрытых не найдена" in await _run(demo_tenant, "/done 99999")
    assert "Не разобрал «100000»" in await _run(demo_tenant, "/done 100000")


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


def _reply_text_message(
    text: str, *, reply_message_id: str, reply_text: str, chat_id: str = STAFF_CHAT
) -> NormalizedMessage:
    """Текст ответом (reply) на сообщение бота."""
    return NormalizedMessage(
        channel="telegram",
        chat_id=chat_id,
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
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c1")
    return sender


async def _seed_notification(tenant_id: uuid.UUID, request_id: uuid.UUID) -> str:
    """Записать «уведомление о заявке» как это делает notifications.py; вернуть его msg id."""
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
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


async def test_note_from_command_tail_is_card_masked(demo_tenant: uuid.UUID) -> None:
    """Примечание из хвоста команды маскируется каноном (spec 0031 §2, NG-3).

    Команда разбирается из сырого текста (#172), но хвост уходит в
    `resolution_note` — в БД и гостю; сырой PAN там существовать не должен.
    """
    request_id = await _make_request(demo_tenant)
    await _run(demo_tenant, f"/start {request_id}")
    await _run(demo_tenant, f"/done {request_id} гость дал карту 4111 1111 1111 1111")
    with tenant_context(demo_tenant):
        updated = await requests_api.get_request(request_id)
    assert updated.resolution_note == "гость дал карту [card ****1111]"


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


async def test_reply_command_never_reaches_another_chats_request(demo_tenant: uuid.UUID) -> None:
    """Команда-реплай двигает заявку СВОЕГО чата (issue #206, spec 0026).

    Номера сообщений Telegram нумеруются внутри чата, поэтому у чатов уборки и
    инженерии они совпадают постоянно. `/done` реплаем в чате уборки обязан
    закрыть уборочную заявку и не тронуть инженерную с тем же номером
    сообщения: `done` терминален, и чужая работа исчезла бы из очереди
    насовсем, а «выполнено» ушло бы не тому гостю.
    """
    housekeeping_chat, maintenance_chat = "1001", "1002"
    housekeeping_request = await _make_request(demo_tenant, "housekeeping")
    maintenance_request = await _make_request(demo_tenant, "maintenance")
    shared_message_id = "77"  # один номер сообщения в обоих чатах — обычное дело
    with tenant_context(demo_tenant):
        housekeeping_dialog = await ensure_conversation("telegram", housekeeping_chat)
        maintenance_dialog = await ensure_conversation("telegram", maintenance_chat)
        for dialog, request_id in (
            (housekeeping_dialog, housekeeping_request),
            (maintenance_dialog, maintenance_request),
        ):
            await record_outbound_message(
                dialog,
                "🔔 Новая заявка",
                "c0",
                external_message_id=shared_message_id,
                idempotency_key=f"staff:request_created:{request_id}",
            )
        for request_id in (housekeeping_request, maintenance_request):
            await requests_api.change_request_status(
                request_id, requests_api.RequestStatus.IN_PROGRESS
            )

        sender = RecordingSender()
        await handle_staff_message(
            housekeeping_dialog,
            _reply_text_message(
                "/done",
                reply_message_id=shared_message_id,
                reply_text="🔔 Новая заявка",
                chat_id=housekeeping_chat,
            ),
            sender=sender,
            correlation_id="c1",
        )
        assert (
            await requests_api.get_request(housekeeping_request)
        ).status is requests_api.RequestStatus.DONE
        assert (
            await requests_api.get_request(maintenance_request)
        ).status is requests_api.RequestStatus.IN_PROGRESS
    assert len(sender.sent) == 1 and sender.sent[0][0] == housekeeping_chat


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

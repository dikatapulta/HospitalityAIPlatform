"""Consent-gate Telegram: без согласия — ни LLM, ни заявок (spec 0029, issue #127).

DoD задачи целиком: без нажатия кнопки LLM не вызывается и заявки не создаются;
версия согласия в БД; смена `consent_version` перезапрашивает согласие; оба пути
покрыты. Заодно — первое касание `/start` (issue #39): приветствие с именем
отеля без вызова LLM.

Канон оформления — test_webhook.py: настоящий `create_app`, ASGI-клиент,
фейк-отправитель и мок-провайдер. Провайдер намеренно НЕ отвечает пустым: если
гейт протечёт, `provider.calls` это покажет.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.common.consent import CONSENT_VERSION, consent_button_label, consent_text
from hospitality.channels.common.guest_turn import RATE_LIMITED_REPLY
from hospitality.channels.common.models import Conversation, Message, MessageDirection
from hospitality.channels.common.store import ensure_conversation, record_consent
from hospitality.channels.telegram.consent import build_consent_callback_data
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import grant_consent
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

TEST_SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": TEST_SECRET}
GUEST_CHAT = 555
CONSENT_MESSAGE_ID = 4242
BOT_REPLY = "Здравствуйте! Чем помочь?"
HOTEL_NAME = "Demo Hotel"  # фикстура demo_tenant


class RecordingSender:
    """Фейк-отправитель: копит отправленное, клавиатуры, тосты и снятия кнопок."""

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
        return str(CONSENT_MESSAGE_ID)

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.toasts.append((callback_id, text))

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


def _text_update(
    update_id: int, text: str = "уберите номер 305", *, language: str | None = None
) -> dict[str, Any]:
    from_user: dict[str, Any] = {"id": 42}
    if language is not None:
        from_user["language_code"] = language
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": GUEST_CHAT},
            "from": from_user,
            "text": text,
        },
    }


def _consent_click(update_id: int, *, version: str = CONSENT_VERSION) -> dict[str, Any]:
    """Payload нажатия кнопки согласия — как его шлёт Telegram."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": 42},
            "data": build_consent_callback_data(version),
            "message": {
                "message_id": CONSENT_MESSAGE_ID,
                "chat": {"id": GUEST_CHAT},
                "text": "текст согласия",
            },
        },
    }


@pytest.fixture
async def gate(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID]]:
    """Вебхук БЕЗ предварительного согласия — гость приходит первый раз."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()
    app = create_app()
    sender = RecordingSender()
    provider = MockLlmProvider(text=BOT_REPLY)
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, provider, demo_tenant
    get_settings.cache_clear()


async def _post(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post("/channels/telegram/webhook", json=payload, headers=AUTH)
    assert response.status_code == 200


async def _messages(tenant_id: uuid.UUID) -> list[Message]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(Message).order_by(Message.created_at)))


async def _consent_row(tenant_id: uuid.UUID) -> Conversation:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row = await session.scalar(
                select(Conversation).where(Conversation.external_id == str(GUEST_CHAT))
            )
    assert row is not None
    return row


async def test_message_before_consent_never_reaches_llm(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Главный инвариант #127: без согласия ни LLM-вызова, ни заявок.

    И ни одного сохранённого входящего: согласие покрывает обработку текстов
    сообщений, значит до него хранить их не на чем (spec 0029 §4).
    """
    client, sender, provider, tenant_id = gate
    await _post(client, _text_update(1))

    assert provider.calls == []
    with tenant_context(tenant_id):
        assert (await requests_api.list_requests(limit=10, offset=0)).items == []

    stored = await _messages(tenant_id)
    assert [row.direction for row in stored] == [MessageDirection.OUTBOUND]
    assert stored[0].text == sender.sent[0][1]
    assert "уберите номер 305" not in [row.text for row in stored]

    # Экран согласия: полный текст + кнопка (все три языка — язык неизвестен).
    chat, text = sender.sent[0]
    assert chat == str(GUEST_CHAT)
    assert text == consent_text(None)
    button = sender.markups[0]["inline_keyboard"][0][0]  # type: ignore[index]
    assert consent_button_label(None) in button["text"]

    # Согласия в БД нет — гость его не давал.
    assert (await _consent_row(tenant_id)).consent_version is None


async def test_button_records_consent_and_greets(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Нажатие кнопки: версия в БД, кнопка снята, приветствие с именем отеля."""
    client, sender, provider, tenant_id = gate
    await _post(client, _text_update(1))
    await _post(client, _consent_click(2))

    row = await _consent_row(tenant_id)
    assert row.consent_version == CONSENT_VERSION
    assert row.consent_at is not None

    assert sender.toasts and sender.toasts[0][0] == "cb-2"
    # Кнопку убрали: нажимать её больше незачем.
    assert sender.keyboard_edits == [(str(GUEST_CHAT), str(CONSENT_MESSAGE_ID), None)]
    greeting = sender.sent[-1][1]
    assert HOTEL_NAME in greeting
    assert provider.calls == []  # приветствие детерминированное, без LLM


async def test_consented_guest_reaches_orchestrator(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Второй путь DoD: после согласия обычный текст идёт в оркестратор как раньше."""
    client, sender, provider, tenant_id = gate
    await _post(client, _text_update(1))
    await _post(client, _consent_click(2))
    await _post(client, _text_update(3, "во сколько завтрак?"))

    assert len(provider.calls) == 1
    assert sender.sent[-1] == (str(GUEST_CHAT), BOT_REPLY)
    # Теперь входящее сохраняется — согласие на обработку текстов получено.
    assert "во сколько завтрак?" in [row.text for row in await _messages(tenant_id)]


async def test_new_consent_version_reprompts(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Смена версии текста возвращает гостя в гейт (spec 0029 §6)."""
    client, sender, provider, tenant_id = gate
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(CHANNEL, str(GUEST_CHAT))
        await record_consent(conversation_id, "2020-01-01-v0")

    await _post(client, _text_update(1))

    assert provider.calls == []
    assert sender.sent[-1][1] == consent_text(None)


async def test_stale_button_does_not_grant_consent(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Кнопка от прежней версии текста согласием на новый текст не считается."""
    client, sender, _provider, tenant_id = gate
    await _post(client, _consent_click(1, version="2020-01-01-v0"))

    assert (await _consent_row(tenant_id)).consent_version is None
    assert sender.sent[-1][1] == consent_text(None)  # показан актуальный экран


async def test_flood_before_consent_yields_single_screen(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Поток сообщений неответившего гостя даёт ОДИН экран (P-8, защита бюджета).

    Ровно то, ради чего гейт «режет ботов-сканеров»: сканер получает один
    исходящий, а не по одному на каждое своё сообщение.
    """
    client, sender, provider, _tenant_id = gate
    for update_id in (1, 2, 3):
        await _post(client, _text_update(update_id, f"сообщение {update_id}"))

    assert provider.calls == []
    assert len(sender.sent) == 1


async def test_start_reshows_screen_before_consent(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """`/start` показывает экран всегда: гость мог удалить сообщение с кнопкой."""
    client, sender, provider, _tenant_id = gate
    await _post(client, _text_update(1, "/start"))
    await _post(client, _text_update(2, "/start deep-link-payload"))
    # Обычное сообщение после команды экрана не добавляет: ключ уже забран
    # первым показом (иначе `/start` + текст давали бы два экрана подряд).
    await _post(client, _text_update(3, "уберите номер 305"))

    assert provider.calls == []
    assert [text for _chat, text in sender.sent] == [consent_text(None)] * 2


async def test_start_after_consent_greets_without_llm(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """DoD issue #39: `/start` отвечает приветствием с именем отеля и без LLM."""
    client, sender, provider, tenant_id = gate
    await grant_consent(tenant_id, GUEST_CHAT)

    await _post(client, _text_update(1, "/start"))

    assert provider.calls == []
    greeting = sender.sent[-1][1]
    assert HOTEL_NAME in greeting
    assert "AI-консьерж" in greeting


async def test_known_language_shows_single_version(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
) -> None:
    """Язык клиента известен → одна версия текста, а не все три (spec 0029 §3)."""
    client, sender, _provider, _tenant_id = gate
    await _post(client, _text_update(1, language="kk-KZ"))

    text = sender.sent[0][1]
    assert text == consent_text("kk")
    assert "Продолжая, вы соглашаетесь" not in text


async def test_start_flood_before_consent_hits_rate_limit(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/start` показывает экран всегда — но не бесконечно: гейт считает те же
    ступени spec 0023, что и ход гостя (иначе сканер получал бы экран на каждое
    своё сообщение)."""
    client, sender, _provider, _tenant_id = gate
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES", "1")
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_WINDOW_SECONDS", "600")
    get_settings.cache_clear()
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.time", SimpleNamespace(time=lambda: 1_000_000.0)
    )

    await _post(client, _text_update(1, "/start"))
    await _post(client, _text_update(2, "/start"))

    texts = [text for _chat, text in sender.sent]
    assert texts == [consent_text(None), RATE_LIMITED_REPLY]


async def test_staff_chat_is_not_gated(
    gate: tuple[AsyncClient, RecordingSender, MockLlmProvider, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Персоналу гейт не показывается: команды сотрудника — не гостевые данные."""
    client, sender, _provider, tenant_id = gate
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(GUEST_CHAT))
    get_settings.cache_clear()

    await _post(client, _text_update(1, "/чепуха"))

    assert consent_text(None) not in [text for _chat, text in sender.sent]
    assert (await _consent_row(tenant_id)).consent_version is None

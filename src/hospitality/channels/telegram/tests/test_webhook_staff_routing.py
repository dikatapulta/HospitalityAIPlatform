"""Граница «кто персонал» при маршрутизации по службам (spec 0026, issue #80).

Развилка `service.process_update` — единственное место, где решается, чьё
сообщение трактуется как команда персонала (двигает чужие заявки), а чьё — как
реплика гостя оркестратору. С появлением чатов служб это уже не сравнение с
одной строкой, а членство в множестве, поэтому граница покрыта отдельно:
чат службы становится staff'ом, а гостевой чат не становится им ни при каком
конфиге (в том числе когда его id — подстрока id служебного чата).

Различить ветки просто по ответу бота: персоналу на неизвестную команду уходит
подсказка «Команды службы…», гостю — реплика консьержа (мок-провайдер).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import grant_consent, set_staff_routing
from hospitality.shared.config import get_settings

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
BOT_REPLY = "Здравствуйте! Чем помочь?"
STAFF_HINT = "Команды службы"

DEFAULT_STAFF_CHAT = "-1000000"
HOUSEKEEPING_CHAT = "-1001234567890"
# Гостевой чат, чей id — подстрока id чата уборки: проверка на строгое равенство
# в множестве staff-чатов, а не на «похожесть» строк.
GUEST_CHAT_SUBSTRING = "100123"
GUEST_CHAT = "555"


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        return "m" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        return None


@pytest.fixture
async def stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, uuid.UUID]]:
    """Вебхук с настроенным дефолтным staff-чатом и фейками отправителя/LLM."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", DEFAULT_STAFF_CHAT)
    get_settings.cache_clear()
    app = create_app()
    sender = RecordingSender()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text=BOT_REPLY)
    # Consent-gate (spec 0029) пройден заранее во всех чатах, которые в этом
    # файле обязаны остаться гостевыми: проверяется граница «кто персонал»,
    # а не согласие.
    for chat in (GUEST_CHAT, GUEST_CHAT_SUBSTRING, HOUSEKEEPING_CHAT):
        await grant_consent(demo_tenant, chat)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, demo_tenant
    get_settings.cache_clear()


async def _say(client: AsyncClient, chat_id: str, text: str, update_id: int) -> None:
    response = await client.post(
        "/channels/telegram/webhook",
        json={
            "update_id": update_id,
            "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text},
        },
        headers=AUTH,
    )
    assert response.status_code == 200


async def test_service_chat_is_treated_as_staff(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Чат службы из конфига — персонал: его текст разбирается как команда."""
    client, sender, tenant_id = stand
    await set_staff_routing(tenant_id, {"housekeeping": HOUSEKEEPING_CHAT})

    await _say(client, HOUSEKEEPING_CHAT, "/чепуха", 1)

    assert len(sender.sent) == 1
    chat_id, reply = sender.sent[0]
    assert chat_id == HOUSEKEEPING_CHAT
    assert STAFF_HINT in reply


async def test_default_chat_stays_staff_with_routing(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Дефолтный чат остаётся персоналом и после появления чатов служб."""
    client, sender, tenant_id = stand
    await set_staff_routing(tenant_id, {"housekeeping": HOUSEKEEPING_CHAT})

    await _say(client, DEFAULT_STAFF_CHAT, "/чепуха", 2)

    assert STAFF_HINT in sender.sent[0][1]


async def test_guest_chat_cannot_become_staff_via_config(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Граница безопасности: конфиг маршрутизации не делает персоналом чужой чат.

    Гость, чей chat_id — подстрока id служебного чата (`100123` внутри
    `-1001234567890`), обязан остаться гостем: проверка — строгое равенство в
    множестве, а не совпадение подстроки. Иначе гость получил бы права
    персонала и двигал чужие заявки командами.
    """
    client, sender, tenant_id = stand
    await set_staff_routing(tenant_id, {"housekeeping": HOUSEKEEPING_CHAT})

    await _say(client, GUEST_CHAT_SUBSTRING, "/чепуха", 3)
    await _say(client, GUEST_CHAT, "/чепуха", 4)

    assert [chat for chat, _ in sender.sent] == [GUEST_CHAT_SUBSTRING, GUEST_CHAT]
    for _chat, reply in sender.sent:
        assert reply == BOT_REPLY  # ответил консьерж, а не разбор команд
        assert STAFF_HINT not in reply


async def test_unreadable_config_degrades_towards_guest(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Конфиг тенанта не задан (онбординг не завершён) → персоналом остаётся
    только дефолтный чат. Деградация в сторону гостя: сотрудник, которому
    ответил консьерж, — неудобство; гость с правами персонала — привилегия."""
    client, sender, _tenant_id = stand  # set_staff_routing намеренно не звался

    await _say(client, HOUSEKEEPING_CHAT, "/чепуха", 5)
    await _say(client, DEFAULT_STAFF_CHAT, "/чепуха", 6)

    assert sender.sent[0][1] == BOT_REPLY  # чат службы без конфига — гость
    assert STAFF_HINT in sender.sent[1][1]  # дефолтный чат — по-прежнему персонал

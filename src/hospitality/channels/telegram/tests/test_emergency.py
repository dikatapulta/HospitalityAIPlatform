"""ЧП-перехват на пути гостя (spec 0034, issue #208, DoD).

Сквозные сценарии на каноне `test_escalation.py`: ASGI (`create_app`) + шина
событий (`deliver_pending_events` — та же доставка outbox, что в проде, но
инлайн) + подписчики-уведомления на одном запоминающем отправителе.

DoD #208: «пожар» не доходит до модели, персонал узнаёт эскалацией, гость
получает утверждённый текст с телефоном ресепшена и 112, а исчерпанный
rate-limit ЧП не глушит.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hospitality.ai import urgency
from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.common.guest_turn import RATE_LIMITED_REPLY
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import RecordingSender, grant_consent
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.events import deliver_pending_events
from tests.conftest import FakeRateLimitRedis

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
STAFF_CHAT = 999
RECEPTION_PHONE = "+7 727 000 00 00"


def _guest_text(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": GUEST_CHAT}, "text": text},
    }


async def _set_reception_phone(tenant_id: uuid.UUID, phone: str | None) -> None:
    """Телефон ресепшена в конфиге тенанта — источник строки текста (§12 п. 5)."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=phone,
            ),
        )


@pytest.fixture
async def stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, MockLlmProvider, FastAPI, uuid.UUID]]:
    """Стенд вебхука: фейк-отправитель, фейк-Redis лимита, провайдер-счётчик."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(STAFF_CHAT))
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES", "2")
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_WINDOW_SECONDS", "600")
    get_settings.cache_clear()
    # Один инстанс на стенд: `lambda: FakeRateLimitRedis()` заводил бы счётчик
    # заново на каждом сообщении, и лимит не срабатывал бы никогда.
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.time", SimpleNamespace(time=lambda: 1_000_000.0)
    )
    provider = MockLlmProvider(text="Здравствуйте! Чем помочь?")
    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    notifications.register(
        sender=sender,
        default_staff_chat_id=str(STAFF_CHAT),
        translate_provider=MockLlmProvider(text="перевод не нужен"),
    )
    await grant_consent(demo_tenant, GUEST_CHAT)
    await _set_reception_phone(demo_tenant, RECEPTION_PHONE)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, provider, app, demo_tenant
    get_settings.cache_clear()


async def _post(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post("/channels/telegram/webhook", json=payload, headers=AUTH)
    assert response.status_code == 200


def _to_guest(sender: RecordingSender) -> list[str]:
    return [text for chat, text in sender.sent if chat == str(GUEST_CHAT)]


def _to_staff(sender: RecordingSender) -> list[str]:
    return [text for chat, text in sender.sent if chat == str(STAFF_CHAT)]


async def test_emergency_never_reaches_the_model_and_answers_approved_text(
    stand: tuple[AsyncClient, RecordingSender, MockLlmProvider, FastAPI, uuid.UUID],
) -> None:
    """DoD: «пожар» → ноль вызовов LLM, утверждённый текст с телефонами."""
    client, sender, provider, _app, _tenant = stand
    await _post(client, _guest_text(1, "у нас в номере пожар!!!"))

    assert provider.calls == []  # модель не звалась ни разу — в этом весь смысл
    assert _to_guest(sender) == [urgency.emergency_reply("ru", RECEPTION_PHONE)]
    assert f"📞 Ресепшен: {RECEPTION_PHONE}" in _to_guest(sender)[0]
    assert "📞 112" in _to_guest(sender)[0]


async def test_emergency_reaches_default_staff_chat(
    stand: tuple[AsyncClient, RecordingSender, MockLlmProvider, FastAPI, uuid.UUID],
) -> None:
    """Обещание «уже передаю персоналу» правдиво (инвариант spec 0022).

    Факт уходит в outbox ДО реплики гостю, а подписчик доставляет его в
    ДЕФОЛТНЫЙ чат — «уровень выше» (spec 0026): у ЧП категории нет.
    """
    client, sender, _provider, _app, _tenant = stand
    await _post(client, _guest_text(1, "мне плохо, вызовите врача"))
    # Гость уже получил ответ, а событие ещё только в outbox — публикация
    # предшествует реплике, иначе сбой публикации сделал бы обещание ложью.
    assert _to_staff(sender) == []

    assert await deliver_pending_events() >= 1
    (staff_text,) = _to_staff(sender)
    assert staff_text.startswith("🚨 ЧП: сообщение гостя")
    assert f"Чат: {GUEST_CHAT}" in staff_text
    assert "Последняя реплика: «мне плохо, вызовите врача»" in staff_text
    assert "чрезвычайной ситуации" in staff_text


async def test_emergency_passes_through_exhausted_rate_limit(
    stand: tuple[AsyncClient, RecordingSender, MockLlmProvider, FastAPI, uuid.UUID],
) -> None:
    """Исчерпанный лимит ЧП не глушит (spec 0034 §4).

    Лимит защищает бюджет LLM, а перехват модель не зовёт: отвечать «подождите
    пару минут» гостю, у которого горит номер, нечем оправдать.
    """
    client, sender, provider, _app, _tenant = stand
    await _post(client, _guest_text(1, "во сколько завтрак?"))
    await _post(client, _guest_text(2, "а обед?"))
    await _post(client, _guest_text(3, "а ужин?"))
    assert _to_guest(sender)[-1] == RATE_LIMITED_REPLY

    await _post(client, _guest_text(4, "в номере дым!"))
    assert _to_guest(sender)[-1] == urgency.emergency_reply("ru", RECEPTION_PHONE)
    assert len(provider.calls) == 2  # перехват модель не звал


async def test_emergency_without_tenant_config_still_answers(
    stand: tuple[AsyncClient, RecordingSender, MockLlmProvider, FastAPI, uuid.UUID],
) -> None:
    """Онбординг не завершён — текст остаётся, но без строки ресепшена.

    Деградация в сторону гостя: номер 112 не зависит ни от какой настройки.
    """
    client, sender, _provider, _app, tenant_id = stand
    await _set_reception_phone(tenant_id, None)
    await _post(client, _guest_text(1, "fire in my room"))
    reply = _to_guest(sender)[-1]
    assert reply == urgency.emergency_reply("en", None)
    assert "Reception" not in reply
    assert "📞 112" in reply

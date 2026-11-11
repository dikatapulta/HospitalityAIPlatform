"""Telegram-гость до и после привязки кодом заселения (spec 0074 §2, #484).

Пробы аудита 26.10: «почините сейф в 402» без Stay не доходит до модели и не
рождает заявку; после привязки заявка уходит в комнату Stay, а не в текст гостя;
ЧП непривязанного — текст ЧП без тревоги персоналу. Канон оформления —
`test_consent.py`: настоящий `create_app`, ASGI-клиент, фейки отправителя и модели.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai import urgency
from hospitality.ai.gateway.api import MockLlmProvider, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.app import create_app
from hospitality.channels.common.models import Message
from hospitality.channels.telegram.binding import parse_code_attempt
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import RecordingSender, bind_chat, grant_consent
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
PROMPT_RU = "Чтобы начать, отправьте одним сообщением номер комнаты и код заселения"
RATE_LIMITED_RU = "Слишком много попыток. Попробуйте позже или обратитесь на ресепшен."
Stand = tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID]


def _text(update_id: int, text: str, *, chat: int = GUEST_CHAT) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat},
            "from": {"id": chat, "language_code": "ru"},
            "text": text,
        },
    }


@pytest.fixture
async def stand(demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stand]:
    """Согласие дано, привязки нет; лимиты — на in-memory Redis (CI без Redis)."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="maintenance", name="Инженерная служба")
        )
    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text="ок")
    await grant_consent(demo_tenant, GUEST_CHAT, bind=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sender, app, demo_tenant
    get_settings.cache_clear()


async def _post(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post("/channels/telegram/webhook", json=payload, headers=AUTH)
    assert response.status_code == 200


def _replies(sender: RecordingSender, chat_id: int = GUEST_CHAT) -> list[str]:
    return [text for chat, text in sender.sent if chat == str(chat_id)]


async def _check_in(tenant_id: uuid.UUID, room: str = "305") -> guests_api.StayCheckInResult:
    with tenant_context(tenant_id):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + timedelta(days=1))
        )
    await deliver_pending_events()
    return result


async def _requests(tenant_id: uuid.UUID) -> list[requests_api.ServiceRequestRead]:
    with tenant_context(tenant_id):
        return (await requests_api.list_requests(limit=10, offset=0)).items


async def _stored_texts(tenant_id: uuid.UUID) -> list[str]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return [text or "" for text in await session.scalars(select(Message.text))]


async def test_unbound_guest_gets_prompt_without_model_or_request(stand: Stand) -> None:
    """Проба К-2: «почините сейф в 402» без Stay — ни модели, ни заявки, подсказка."""
    client, sender, app, tenant_id = stand
    provider = MockLlmProvider(text="не должно прозвучать")
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    await _post(client, _text(1, "почините сейф в 402"))

    assert provider.calls == []
    assert await _requests(tenant_id) == []
    (reply,) = _replies(sender)
    assert reply.startswith(PROMPT_RU) and "305 123-456" in reply


async def test_unbound_emergency_gets_text_without_alarm(stand: Stand) -> None:
    """ЧП непривязанного: утверждённый текст ЧП, но ни эскалации, ни модели (§3)."""
    client, sender, app, _tenant_id = stand
    provider = MockLlmProvider(text="не должно прозвучать")
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    await _post(client, _text(1, "пожар в коридоре!"))

    assert _replies(sender) == [urgency.emergency_reply("ru", None)]
    assert provider.calls == []
    assert await deliver_pending_events() == 0  # тревоги персоналу в outbox нет


async def test_code_binds_and_request_goes_to_stay_room(stand: Stand) -> None:
    """Код подошёл → «Готово»; заявка привязанного уходит в 305, а не в «402» из текста."""
    client, sender, app, tenant_id = stand
    stay = await _check_in(tenant_id, "305")
    proposal = ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "maintenance",
            "summary": "починить сейф",
            "room_number": "402",
            "confirmation_question": "Оформить ремонт сейфа?",
        },
    )
    verdict = ToolCall(
        id="toolu_2",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": "Передал инженерам."},
    )
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[proposal]), MockTurn(tool_calls=[verdict])]
    )
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    code = guests_api.format_access_code(stay.access_code)

    await _post(client, _text(1, f"комната 305, код {code}"))
    await _post(client, _text(2, "почините сейф в 402"))
    await _post(client, _text(3, "да"))

    assert _replies(sender)[0] == "Готово, номер 305 подключён. Напишите, чем помочь."
    (request,) = await _requests(tenant_id)
    assert request.room_number == "305"
    assert request.stay_id == stay.stay.id
    stored = await _stored_texts(tenant_id)
    assert "305 [код заселения]" in stored  # §2.1: код в истории не хранится
    assert not any(code in text or stay.access_code in text for text in stored)


async def test_wrong_code_is_rejected_and_guest_stays_unbound(stand: Stand) -> None:
    client, sender, _app, tenant_id = stand
    await _check_in(tenant_id, "305")

    await _post(client, _text(1, "305 000-000"))
    await _post(client, _text(2, "привет"))

    rejected, prompt = _replies(sender)
    assert rejected.startswith("Код не подошёл.")
    assert prompt.startswith(PROMPT_RU)


async def test_card_number_is_not_parsed_as_code_attempt(
    stand: Stand, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ревью PR #912, Б-1: попытка разбирается из маскированного текста (NG-3).

    Из сырого «карта 4111…» разбор вынул бы «комнату» 4111111111 и код 111111:
    первые 10 цифр карты — в лог `guest_code_rejected` и в ключ лимита по комнате.
    """
    client, sender, _app, _tenant_id = stand
    configure_logging()
    capsys.readouterr()

    async def no_bcrypt(_start: guests_api.GuestSessionStart) -> None:
        raise AssertionError("непопытка дошла до проверки кода")

    monkeypatch.setattr(guests_api, "start_guest_session", no_bcrypt)

    await _post(client, _text(1, "карта 4111111111111111"))

    (reply,) = _replies(sender)
    assert reply.startswith(PROMPT_RU)
    assert "4111111111" not in capsys.readouterr().out


async def test_code_attempts_are_limited_per_room(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предел попыток исчерпан — верный код уже не пускает (лимит до bcrypt, §2.1)."""
    client, sender, _app, tenant_id = stand
    monkeypatch.setenv("GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS", "1")
    get_settings.cache_clear()
    stay = await _check_in(tenant_id, "305")

    await _post(client, _text(1, "305 000-000"))
    await _post(client, _text(2, f"305 {stay.access_code}"))

    assert _replies(sender)[-1] == RATE_LIMITED_RU
    with tenant_context(tenant_id):
        assert await guests_api.count_stay_sessions(stay.stay.id) == 0


async def test_room_code_limit_is_not_bypassed_from_another_chat(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ступень «на комнату» держит перебор кода из многих чатов, соседнюю комнату — нет.

    Проба мутаций #1052: без ступени по комнате тест выше зелёный — обе его попытки
    из одного чата, и отказывает ступень по чату. Здесь второй чат упирается только
    в комнату, а третий на соседней 306 проходит: предел — на комнату, а не на всех.
    """
    client, sender, _app, tenant_id = stand
    monkeypatch.setenv("GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS", "1")
    get_settings.cache_clear()
    stay = await _check_in(tenant_id, "305")
    neighbour = await _check_in(tenant_id, "306")
    second_chat, third_chat = GUEST_CHAT + 1, GUEST_CHAT + 2
    await grant_consent(tenant_id, second_chat, bind=False)
    await grant_consent(tenant_id, third_chat, bind=False)

    await _post(client, _text(1, "305 000-000"))
    await _post(client, _text(2, f"305 {stay.access_code}", chat=second_chat))
    await _post(client, _text(3, f"306 {neighbour.access_code}", chat=third_chat))

    assert _replies(sender, second_chat) == [RATE_LIMITED_RU]
    assert _replies(sender, third_chat) == ["Готово, номер 306 подключён. Напишите, чем помочь."]
    with tenant_context(tenant_id):
        assert await guests_api.count_stay_sessions(stay.stay.id) == 0
        assert await guests_api.count_stay_sessions(neighbour.stay.id) == 1


async def test_checked_out_guest_gets_prompt_again(stand: Stand) -> None:
    """После выезда — снова подсказка, модель не зовётся (ADR-008 Q8)."""
    client, sender, app, tenant_id = stand
    provider = MockLlmProvider(text="не должно прозвучать")
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    with tenant_context(tenant_id):
        stay_id = await bind_chat(GUEST_CHAT, room="305")
        await guests_api.check_out(stay_id)
    await deliver_pending_events()

    await _post(client, _text(1, "принесите полотенца"))

    assert provider.calls == []
    assert _replies(sender)[0].startswith(PROMPT_RU)


async def test_start_adds_prompt_only_for_unbound_guest(stand: Stand) -> None:
    client, sender, _app, tenant_id = stand
    await _post(client, _text(1, "/start"))
    with tenant_context(tenant_id):
        await bind_chat(GUEST_CHAT, room="305")
    await _post(client, _text(2, "/start"))

    unbound, bound = _replies(sender)
    assert PROMPT_RU in unbound
    assert PROMPT_RU not in bound and "AI-консьерж" in bound


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("305 482-913", ("305", "482-913")),
        ("305 482913", ("305", "482913")),
        ("305 482 913", ("305", "482 913")),
        ("комната 305, код 482-913.", ("305", "482-913")),
        ("Room 12A code 482913", ("12A", "482913")),
    ],
)
def test_code_attempt_is_parsed(text: str, expected: tuple[str, str]) -> None:
    attempt = parse_code_attempt(text)
    assert attempt is not None
    assert (attempt.room_number, attempt.code) == expected


@pytest.mark.parametrize(
    "text", ["почините сейф в 402", "482-913", "во сколько завтрак?", "", "305 48-913"]
)
def test_plain_message_is_not_an_attempt(text: str) -> None:
    assert parse_code_attempt(text) is None

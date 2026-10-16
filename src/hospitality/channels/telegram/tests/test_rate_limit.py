"""Rate-limit гостевого чата на уровне вебхука (issue #41, spec 0023, DoD).

Канон HTTP-теста с БД (как test_webhook): ASGI-клиент поверх `create_app`,
отправитель и LLM-провайдер — фейки. Счётчик — `FakeRateLimitRedis` (юнит-тесты
CI идут без живого Redis); время лимитера заморожено, чтобы прогон на границе
окна/UTC-суток не сдвигал bucket посреди теста.

DoD #41: (N+1)-е сообщение в окне НЕ создаёт LLM-вызов (`provider.calls` не
растёт) и получает отказ; входящее при этом сохранено (история честная).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.common.chat_limits import (
    DAILY_LIMIT_REPLY,
    MAX_GUEST_MESSAGE_CHARS,
    MESSAGE_TOO_LONG_REPLY,
    RATE_LIMITED_REPLY,
)
from hospitality.channels.common.guest_turn import UNSUPPORTED_REPLY
from hospitality.channels.common.models import Message, MessageDirection
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import grant_consent
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

TEST_SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": TEST_SECRET}
CHAT_ID = 555
BOT_REPLY = "Здравствуйте! Чем помочь?"


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        return str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        return None


def _text_update(update_id: int, text: str = "во сколько завтрак?") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": CHAT_ID}, "text": text},
    }


def _photo_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": CHAT_ID}, "photo": [{"file_id": "x"}]},
    }


@asynccontextmanager
async def _stand(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages: int,
    per_day: int = 200,
    budget_share: float = 0.0,
    redis_down: bool = False,
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, MockLlmProvider]]:
    """Стенд вебхука с лимитами из аргументов и фейк-Redis вместо настоящего.

    Денежная ступень по умолчанию выключена (`budget_share=0`): тесты счётных
    ступеней не должны зависеть от цены фейкового вызова.
    """
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("GUEST_CHAT_DAILY_BUDGET_SHARE", str(budget_share))
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES", str(messages))
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_WINDOW_SECONDS", "600")
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES_PER_DAY", str(per_day))
    get_settings.cache_clear()
    fake_redis = FakeRateLimitRedis()
    fake_redis.fail = redis_down
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    # Замороженное время: настоящее могло бы пересечь границу bucket'а посреди
    # теста (окно 600 — раз в 10 минут, дневная ступень — в полночь UTC).
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.time", SimpleNamespace(time=lambda: 1_000_000.0)
    )
    provider = MockLlmProvider(text=BOT_REPLY)
    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, sender, provider
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _consented(demo_tenant: uuid.UUID) -> None:
    """Consent-gate (spec 0029) пройден заранее: файл про ступени лимита.

    Autouse, а не строка в `_stand`: стенд — контекст-менеджер без тенанта, а
    согласие живёт на диалоге тенанта.
    """
    await grant_consent(demo_tenant, CHAT_ID)


async def _post(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post("/channels/telegram/webhook", json=payload, headers=AUTH)
    assert response.status_code == 200


async def _inbound_texts(tenant_id: uuid.UUID) -> list[str | None]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(Message)
                .where(Message.direction == MessageDirection.INBOUND)
                .order_by(Message.created_at)
            )
            return [row.text for row in rows]


async def test_message_over_limit_gets_refusal_without_llm_call(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD #41: (N+1)-е сообщение — отказ без LLM-вызова; история честная;
    дальнейший спам в окне дропается молча (без новых исходящих)."""
    async with _stand(monkeypatch, messages=2) as (client, sender, provider):
        await _post(client, _text_update(1))
        await _post(client, _text_update(2))
        assert len(provider.calls) == 2
        assert sender.sent == [(str(CHAT_ID), BOT_REPLY)] * 2

        await _post(client, _text_update(3, "а обед?"))
        assert len(provider.calls) == 2  # LLM не вызывался
        assert sender.sent[-1] == (str(CHAT_ID), RATE_LIMITED_REPLY)
        # Входящее сохранено, хотя ход отклонён — история диалога честная.
        assert await _inbound_texts(demo_tenant) == [
            "во сколько завтрак?",
            "во сколько завтрак?",
            "а обед?",
        ]

        await _post(client, _text_update(4))
        assert len(provider.calls) == 2
        assert len(sender.sent) == 3  # отказ был один — дальше молча
        # Отклонения видны в метрике по тенанту и ступени (P-10).
        assert (
            REGISTRY.get_sample_value(
                "guest_rate_limited_total",
                {"tenant_id": str(demo_tenant), "scope": "guest_chat_window"},
            )
            == 2.0
        )


async def test_daily_cap_replies_with_its_own_text(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дневная ступень отклоняет своим текстом («сегодня всё», не «пара минут»)."""
    async with _stand(monkeypatch, messages=100, per_day=1) as (client, sender, provider):
        await _post(client, _text_update(1))
        await _post(client, _text_update(2))
        assert len(provider.calls) == 1
        assert sender.sent[-1] == (str(CHAT_ID), DAILY_LIMIT_REPLY)


async def test_window_tier_is_silent_after_daily_cap(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После дневного потолка window-ступень молчит: «подождите пару минут» было
    бы ложью (ждать до завтра) и рождало бы исходящее на каждое новое окно
    (ревью PR #104)."""
    async with _stand(monkeypatch, messages=2, per_day=1) as (client, sender, provider):
        await _post(client, _text_update(1))
        await _post(client, _text_update(2))  # дневной потолок: отказ «сегодня всё»
        assert sender.sent[-1] == (str(CHAT_ID), DAILY_LIMIT_REPLY)

        # Третье сообщение — первое превышение window-ступени (2+1), но дневной
        # потолок уже сработал: никакого нового исходящего и никакого LLM.
        await _post(client, _text_update(3))
        assert len(provider.calls) == 1
        assert len(sender.sent) == 2
        assert RATE_LIMITED_REPLY not in [text for _, text in sender.sent]


async def test_window_rejected_messages_still_fill_daily_counter(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отклонённые всплеск-ступенью сообщения наполняют дневной счётчик (spec 0023):
    иначе спамер получал бы свежий дневной запас после каждого окна."""
    async with _stand(monkeypatch, messages=1, per_day=3) as (client, sender, provider):
        await _post(client, _text_update(1))  # LLM-ход (daily=1, window=1)
        await _post(client, _text_update(2))  # window: первый отказ (daily=2)
        await _post(client, _text_update(3))  # window: молча (daily=3)
        assert len(provider.calls) == 1
        assert sender.sent[-1] == (str(CHAT_ID), RATE_LIMITED_REPLY)

        # Четвёртое пробивает дневной потолок (3+1) — значит, отклонённые
        # window-ступенью сообщения 2 и 3 его действительно наполнили.
        await _post(client, _text_update(4))
        assert sender.sent[-1] == (str(CHAT_ID), DAILY_LIMIT_REPLY)
        assert len(provider.calls) == 1


async def test_unavailable_redis_fails_open(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis упал → диалог работает без лимита (spec 0023: отказ хуже перерасхода)."""
    async with _stand(monkeypatch, messages=1, redis_down=True) as (client, sender, provider):
        await _post(client, _text_update(1))
        await _post(client, _text_update(2))
        assert len(provider.calls) == 2
        assert sender.sent == [(str(CHAT_ID), BOT_REPLY)] * 2


async def test_unsupported_messages_do_not_consume_limit(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не-текст отвечается статически без LLM и не тратит лимит текстовых ходов."""
    async with _stand(monkeypatch, messages=2) as (client, sender, provider):
        await _post(client, _photo_update(1))
        await _post(client, _photo_update(2))
        assert sender.sent == [(str(CHAT_ID), UNSUPPORTED_REPLY)] * 2

        await _post(client, _text_update(3))
        await _post(client, _text_update(4))
        assert len(provider.calls) == 2  # оба текста дошли до оркестратора
        assert sender.sent[-1] == (str(CHAT_ID), BOT_REPLY)


async def test_duplicate_update_does_not_consume_limit(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор вебхука (тот же update_id) отсеян идемпотентностью ДО лимита (P-8)."""
    async with _stand(monkeypatch, messages=2) as (client, sender, provider):
        await _post(client, _text_update(1))
        await _post(client, _text_update(1))  # дубль доставки Telegram
        await _post(client, _text_update(2))
        assert len(provider.calls) == 2
        assert sender.sent == [(str(CHAT_ID), BOT_REPLY)] * 2


# Цена фейкового вызова: MockLlmProvider по умолчанию — 200 входных и 100
# выходных токенов модели claude-opus-4-8 ($5/$25 за MTok) = $0,0035. Доля
# 0,0007 от $10 — потолок $0,007, ровно два хода.
_TWO_CALLS_SHARE = 0.0007


async def test_spend_ceiling_refuses_without_llm_call(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Денежная ступень (#404, spec 0023 §5): чат, потративший свою долю бюджета
    тенанта, дальше LLM не зовёт; отказ «сегодня всё» — один раз, дальше молча."""
    async with _stand(monkeypatch, messages=100, budget_share=_TWO_CALLS_SHARE) as (
        client,
        sender,
        provider,
    ):
        await _post(client, _text_update(1))
        await _post(client, _text_update(2))
        assert len(provider.calls) == 2

        await _post(client, _text_update(3))
        assert len(provider.calls) == 2  # потолок набран ценой двух ходов
        assert sender.sent[-1] == (str(CHAT_ID), DAILY_LIMIT_REPLY)

        await _post(client, _text_update(4))
        assert len(provider.calls) == 2
        assert len(sender.sent) == 3  # отказ был один
        assert (
            REGISTRY.get_sample_value(
                "guest_rate_limited_total",
                {"tenant_id": str(demo_tenant), "scope": "guest_chat_spend"},
            )
            == 2.0
        )


async def test_spend_ceiling_fails_open_without_redis(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis упал — денежная ступень, как и счётные, пропускает (spec 0023 §2)."""
    async with _stand(
        monkeypatch, messages=100, budget_share=_TWO_CALLS_SHARE, redis_down=True
    ) as (client, sender, provider):
        for update_id in range(1, 4):
            await _post(client, _text_update(update_id))
        assert len(provider.calls) == 3


async def test_too_long_message_is_refused_and_kept_out_of_history(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предел длины (sec L7, spec 0023 §5): длиннее предела — отказ без LLM, и
    следующий ход не тащит длинный текст в историю модели; ровно предел — проходит."""
    too_long = "а" * (MAX_GUEST_MESSAGE_CHARS + 1)
    at_limit = "б" * MAX_GUEST_MESSAGE_CHARS
    async with _stand(monkeypatch, messages=100) as (client, sender, provider):
        await _post(client, _text_update(1, too_long))
        assert provider.calls == []
        assert sender.sent == [(str(CHAT_ID), MESSAGE_TOO_LONG_REPLY)]
        # Входящее сохранено — история в БД честная, как у отказа по лимиту.
        assert await _inbound_texts(demo_tenant) == [too_long]

        await _post(client, _text_update(2, at_limit))
        assert len(provider.calls) == 1
        contents = [message.content for message in provider.calls[0].messages]
        assert too_long not in contents
        assert contents[-1] == at_limit


async def test_emergency_in_long_message_is_still_intercepted(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ЧП-перехват стоит раньше предела длины (spec 0034): «пожар» в длинном
    тексте доходит до персонала, а гость не получает «напишите покороче»."""
    long_emergency = "пожар! " + "а" * MAX_GUEST_MESSAGE_CHARS
    async with _stand(monkeypatch, messages=100) as (client, sender, provider):
        await _post(client, _text_update(1, long_emergency))
        assert provider.calls == []
        assert MESSAGE_TOO_LONG_REPLY not in [text for _, text in sender.sent]
        assert sender.sent  # гость получил ответ перехвата

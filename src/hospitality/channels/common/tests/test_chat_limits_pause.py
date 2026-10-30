"""Статус паузы лимита вместо тишины (issue #222, spec 0023 §3).

Отказ лимита уходит репликой один раз на окно; на остальных сообщениях окна
канал, передавший `pause`, получает текст той же ступени — web показывает его
статусом. Без БД: счётчик — `FakeRateLimitRedis`, время лимитера заморожено.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hospitality.channels.common.chat_limits import (
    DAILY_LIMIT_REPLY,
    RATE_LIMITED_REPLY,
    record_chat_spend,
    refuse_if_rate_limited,
)
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

KEY = "stay-1"


class Inbox:
    """Что канал получил за ход: реплики и статусы паузы."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.pauses: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def pause(self, text: str) -> None:
        self.pauses.append(text)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Фейк-Redis, замороженное время, денежная ступень выключена по умолчанию."""
    monkeypatch.setenv("GUEST_CHAT_DAILY_BUDGET_SHARE", "0")
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_WINDOW_SECONDS", "600")
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.time", SimpleNamespace(time=lambda: 1_000_000.0)
    )
    get_settings.cache_clear()
    with tenant_context(uuid.uuid4()):
        yield monkeypatch
    get_settings.cache_clear()


async def _send(inbox: Inbox) -> bool:
    return await refuse_if_rate_limited(
        KEY, external_id="guest", reply=inbox.reply, pause=inbox.pause
    )


def _set_limits(monkeypatch: pytest.MonkeyPatch, *, window: int, per_day: int) -> None:
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES", str(window))
    monkeypatch.setenv("GUEST_CHAT_RATE_LIMIT_MESSAGES_PER_DAY", str(per_day))
    get_settings.cache_clear()


async def test_refusal_once_then_pause_on_every_silent_message(
    limits: pytest.MonkeyPatch,
) -> None:
    _set_limits(limits, window=1, per_day=200)
    inbox = Inbox()

    assert await _send(inbox) is False
    assert await _send(inbox) is True  # первое превышение — отказ репликой
    assert (inbox.replies, inbox.pauses) == ([RATE_LIMITED_REPLY], [])

    assert await _send(inbox) is True
    assert await _send(inbox) is True
    # Реплика по-прежнему одна (N сообщений не рождают N исходящих), а каждое
    # молчаливое сообщение получает статус той же ступени.
    assert inbox.replies == [RATE_LIMITED_REPLY]
    assert inbox.pauses == [RATE_LIMITED_REPLY, RATE_LIMITED_REPLY]


async def test_pause_after_daily_cap_says_today_not_couple_of_minutes(
    limits: pytest.MonkeyPatch,
) -> None:
    """Статус берёт текст первой сработавшей ступени, как и реплика (ревью PR #104):
    после дневного потолка «пара минут» была бы ложью."""
    _set_limits(limits, window=2, per_day=1)
    inbox = Inbox()

    await _send(inbox)
    await _send(inbox)  # дневной потолок: отказ «сегодня всё»
    await _send(inbox)  # первое превышение window, но дневной уже молчит

    assert inbox.replies == [DAILY_LIMIT_REPLY]
    assert inbox.pauses == [DAILY_LIMIT_REPLY]


async def test_spend_ceiling_pause_says_today(limits: pytest.MonkeyPatch) -> None:
    limits.setenv("GUEST_CHAT_DAILY_BUDGET_SHARE", "0.1")  # потолок чата — $1
    _set_limits(limits, window=100, per_day=200)
    await record_chat_spend(KEY, Decimal("2"))
    inbox = Inbox()

    await _send(inbox)
    await _send(inbox)

    assert inbox.replies == [DAILY_LIMIT_REPLY]
    assert inbox.pauses == [DAILY_LIMIT_REPLY]


async def test_without_pause_sender_silence_stays(limits: pytest.MonkeyPatch) -> None:
    """Telegram `pause` не передаёт: лишнее сообщение бота там — спам в ответ."""
    _set_limits(limits, window=1, per_day=200)
    inbox = Inbox()

    for _ in range(4):
        await refuse_if_rate_limited(KEY, external_id="guest", reply=inbox.reply)

    assert inbox.replies == [RATE_LIMITED_REPLY]

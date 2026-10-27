"""Ввод кода заселения `POST /g/{slug}/{room}/session`: ступень лимита по IP и
валидация комнаты до лимитера (spec 0027 §3.3; аудит 26.10 К-1, issue #483).

Ступень по (tenant, room) и fail-open — в `test_web_chat.py`. Счётчики — на
`FakeRateLimitRedis` (канон 0023): в CI живого Redis нет, без подмены лимит
уходит в fail-open и тест зависит от окружения.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient, Response

from hospitality.app import create_app
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.modules.guests import api as guests_api
from hospitality.shared.config import get_settings
from tests.conftest import FakeRateLimitRedis


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        yield client


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRateLimitRedis]:
    """Один фейк на тест: счётчик обязан копиться между запросами."""
    fake = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    yield fake
    get_settings.cache_clear()


@pytest.fixture
def domain_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Шпион на домене: какие комнаты дошли до `start_guest_session` — и до bcrypt."""
    calls: list[str] = []
    original = guests_api.start_guest_session

    async def spy(data: guests_api.GuestSessionStart) -> guests_api.GuestSessionGrant | None:
        calls.append(data.room_number)
        return await original(data)

    monkeypatch.setattr(guests_api, "start_guest_session", spy)
    return calls


async def _attempt(client: AsyncClient, room: str, code: str) -> Response:
    return await client.post(f"/g/{HOTEL_SLUG}/{room}/session", json={"code": code})


async def test_ip_step_stops_room_hopping_before_bcrypt(
    client: AsyncClient,
    web_hotel: WebHotel,
    limits: FakeRateLimitRedis,
    domain_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проба аудита: поток выдуманных комнат с одного адреса.

    Ключ комнаты считает каждую строку новым бакетом и потока не видит; ключ IP
    отсекает третью попытку ДО ключа комнаты и ДО домена (bcrypt). Верный код
    после этого не помогает: ступень тратится попыткой, а не неудачей.
    """
    monkeypatch.setenv("GUEST_CODE_VERIFY_IP_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    statuses = [
        (await _attempt(client, room, "WRONG1")).status_code for room in ("901", "902", "903")
    ]
    assert statuses == [403, 403, 429]
    blocked = await _attempt(client, ROOM, web_hotel.access_code)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "ERR-WEB-004"
    assert domain_calls == ["901", "902"]
    room_keys = [key for key in limits.counters if ":guest_code_verify:" in key]
    ip_keys = [key for key in limits.counters if ":guest_code_verify_ip:" in key]
    assert len(room_keys) == 2  # 903 и 101 до ключа комнаты не дошли
    assert len(ip_keys) == 1 and limits.counters[ip_keys[0]] == 4


async def test_room_is_validated_before_limiter_and_bcrypt(
    client: AsyncClient,
    web_hotel: WebHotel,
    limits: FakeRateLimitRedis,
    domain_calls: list[str],
) -> None:
    """Комната длиннее потолка схемы — 422 на границе роутера, пустая после strip —
    422 из сервиса: ни ключа Redis в тысячи символов, ни bcrypt, ни 500 с телом
    запроса в Sentry. Ровно 20 символов — обычная попытка."""
    for room in ("x" * 21, "x" * 5000, "%20%20"):
        response = await _attempt(client, room, "WRONG1")
        assert response.status_code == 422, (room[:30], response.text)
        assert response.json()["error"]["code"] == "ERR-PLATFORM-002"
    assert limits.counters == {}
    assert domain_calls == []
    boundary = await _attempt(client, "x" * 20, "WRONG1")
    assert boundary.status_code == 403
    assert domain_calls == ["x" * 20]

"""Маршрут одноразовой QR-ссылки `/w/{slug}/b/{token}` (spec 0033 §6/§10).

Redis bind-ссылок подменяется фейком через `bindlink.create_redis_client`
(строковый monkeypatch — внутренности guests напрямую не импортируются, R-5).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, WebHotel
from hospitality.modules.guests import api as guests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeBindLinkRedis, FakeRateLimitRedis

BASE = f"/w/{HOTEL_SLUG}/b"


@pytest.fixture
def bind_redis(monkeypatch: pytest.MonkeyPatch) -> FakeBindLinkRedis:
    fake = FakeBindLinkRedis()
    monkeypatch.setattr("hospitality.modules.guests.bindlink.create_redis_client", lambda: fake)
    return fake


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        yield client


async def _issue_token(web_hotel: WebHotel) -> str:
    with tenant_context(web_hotel.tenant_id):
        return await guests_api.issue_bind_link(web_hotel.stay_id)


async def test_bind_page_shows_consent_line(
    client: AsyncClient, web_hotel: WebHotel, bind_redis: FakeBindLinkRedis
) -> None:
    """GET — consent-строка v3 и кнопка; токен страницей НЕ потребляется."""
    token = await _issue_token(web_hotel)
    response = await client.get(f"{BASE}/{token}")
    assert response.status_code == 200
    assert CONSENT_VERSION in response.text
    assert "Продолжить" in response.text  # кнопка = согласие (spec 0029)
    assert "/legal/privacy" in response.text
    # Токен не потрачен: GET — не согласие.
    assert len(bind_redis.values) == 1


async def test_bind_page_unknown_hotel_is_404(client: AsyncClient, web_hotel: WebHotel) -> None:
    response = await client.get(f"/w/no-such-hotel/b/{uuid.uuid4().hex}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-WEB-001"


async def test_bind_flow_grants_working_chat_session(
    client: AsyncClient, web_hotel: WebHotel, bind_redis: FakeBindLinkRedis
) -> None:
    """Нажатие кнопки: cookie + chat_url; сессия работает в обычном чате
    (привязка — тем же путём, что ввод кода); повтор токена — отказ."""
    token = await _issue_token(web_hotel)
    response = await client.post(f"{BASE}/{token}/session")
    assert response.status_code == 200
    body = response.json()
    assert body["room_number"] == "101"
    assert body["chat_url"] == f"/g/{HOTEL_SLUG}/101"
    assert "guest_session" in response.cookies

    history = await client.get(f"/g/{HOTEL_SLUG}/101/messages")
    assert history.status_code == 200

    reuse = await client.post(f"{BASE}/{token}/session")
    assert reuse.status_code == 403
    assert reuse.json()["error"]["code"] == "ERR-GUESTS-006"


async def test_expired_link_suggests_code_entry(
    client: AsyncClient, web_hotel: WebHotel, bind_redis: FakeBindLinkRedis
) -> None:
    token = await _issue_token(web_hotel)
    bind_redis.advance(guests_api.BIND_LINK_TTL_SECONDS + 1)
    response = await client.post(f"{BASE}/{token}/session")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "ERR-GUESTS-006"
    assert "код" in error["message"]  # переход на обычный ввод кода (spec 0033 §6)


async def test_consume_is_rate_limited_by_ip(
    client: AsyncClient,
    web_hotel: WebHotel,
    bind_redis: FakeBindLinkRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Один инстанс на тест: счётчик обязан копиться между запросами.
    fake_limits = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_limits)
    monkeypatch.setenv("GUEST_BIND_LINK_CONSUME_RATE_LIMIT_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        first = await client.post(f"{BASE}/{uuid.uuid4().hex}/session")
        assert first.status_code == 403  # мусорный токен, но попытка учтена
        second = await client.post(f"{BASE}/{uuid.uuid4().hex}/session")
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "ERR-WEB-005"
    finally:
        get_settings.cache_clear()

"""Маршрут ссылки привязки с талона `/w/{slug}/b/{token}` (spec 0033 §6/§10).

Ссылка живёт в Postgres и действует до выезда (#354): многоразова, гаснет
перевыпуском кода и выездом — все исходы проверяются через `guests_api` (R-5).
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
from tests.conftest import FakeRateLimitRedis

BASE = f"/w/{HOTEL_SLUG}/b"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        yield client


async def _issue_token(web_hotel: WebHotel) -> str:
    with tenant_context(web_hotel.tenant_id):
        return await guests_api.issue_bind_link(web_hotel.stay_id)


async def test_bind_page_shows_consent_line(client: AsyncClient, web_hotel: WebHotel) -> None:
    """GET — consent-строка v3 и кнопка; сессии страница НЕ рождает."""
    token = await _issue_token(web_hotel)
    response = await client.get(f"{BASE}/{token}")
    assert response.status_code == 200
    assert CONSENT_VERSION in response.text
    assert "Продолжить" in response.text  # кнопка = согласие (spec 0029)
    assert "/legal/privacy" in response.text
    # GET — не согласие: привязок у Stay нет.
    with tenant_context(web_hotel.tenant_id):
        assert await guests_api.count_stay_sessions(web_hotel.stay_id) == 0


async def test_bind_page_unknown_hotel_is_404(client: AsyncClient, web_hotel: WebHotel) -> None:
    response = await client.get(f"/w/no-such-hotel/b/{uuid.uuid4().hex}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-WEB-001"


async def test_bind_flow_grants_working_chat_session(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Нажатие кнопки: cookie + chat_url; сессия работает в обычном чате
    (привязка — тем же путём, что ввод кода); повторное сканирование того же
    талона тоже входит — ссылка многоразова до выезда."""
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
    assert reuse.status_code == 200
    with tenant_context(web_hotel.tenant_id):
        assert await guests_api.count_stay_sessions(web_hotel.stay_id) == 2


async def test_link_after_checkout_sends_guest_to_reception(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    token = await _issue_token(web_hotel)
    with tenant_context(web_hotel.tenant_id):
        await guests_api.check_out(web_hotel.stay_id)
    response = await client.post(f"{BASE}/{token}/session")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "ERR-GUESTS-006"
    assert "ресепшен" in error["message"]
    assert "guest_session" not in response.cookies


async def test_overlong_token_is_rejected_at_the_boundary(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Токен длиннее потолка схемы — 422 на границе, а не 500 из сервиса."""
    response = await client.post(f"{BASE}/{'x' * 200}/session")
    assert response.status_code == 422


async def test_bind_is_rate_limited_by_ip(
    client: AsyncClient,
    web_hotel: WebHotel,
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

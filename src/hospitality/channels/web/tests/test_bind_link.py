"""Маршрут ссылки привязки с листка `/w/{slug}/b/{token}` (spec 0033 §6/§10).

Ссылка живёт в Postgres и действует до выезда (#354): многоразова, гаснет
перевыпуском кода и выездом — все исходы проверяются через `guests_api` (R-5).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response

from hospitality.app import create_app
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, WebHotel
from hospitality.modules.guests import api as guests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
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
    assert "<title>Demo Hotel</title>" in response.text  # шапка с отелем, как у чата (#746)
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
    листка тоже входит — ссылка многоразова до выезда."""
    token = await _issue_token(web_hotel)
    response = await client.post(f"{BASE}/{token}/session")
    assert response.status_code == 200
    body = response.json()
    assert body["room_number"] == "101"
    assert body["chat_url"] == f"/g/{HOTEL_SLUG}/101"
    assert response.cookies.get("guest_session", path=f"/g/{HOTEL_SLUG}")

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


def _cookie_max_ages(response: Response) -> dict[str, int]:
    """Max-Age cookie `guest_session` по Path: `{"/g/…": N, "/w/…": N}`."""
    ages: dict[str, int] = {}
    for header in response.headers.get_list("set-cookie"):
        path = re.search(r"Path=([^;]+)", header)
        age = re.search(r"Max-Age=(\d+)", header)
        if header.startswith("guest_session=") and path and age:
            ages[path.group(1)] = int(age.group(1))
    return ages


async def test_cookie_lives_a_day_past_check_out_on_both_paths(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Max-Age — срок + 24 ч на обоих путях привязки (spec 0060 §2.5): поздний
    выезд, поставленный после привязки, не выбрасывает cookie в расчётный час."""
    with tenant_context(web_hotel.tenant_id):
        stay = await guests_api.get_active_stay(web_hotel.stay_id)
    assert stay is not None
    expected = int((stay.check_out_at + timedelta(hours=24) - utc_now()).total_seconds())

    by_link = await client.post(f"{BASE}/{await _issue_token(web_hotel)}/session")
    by_code = await client.post(
        f"/g/{HOTEL_SLUG}/101/session", json={"code": web_hotel.access_code}
    )
    for response in (by_link, by_code):
        assert response.status_code == 200, response.text
        ages = _cookie_max_ages(response)
        assert set(ages) == {f"/g/{HOTEL_SLUG}", f"/w/{HOTEL_SLUG}"}
        for age in ages.values():
            assert expected - 60 <= age <= expected


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


async def test_successful_binds_do_not_spend_ip_budget(
    client: AsyncClient,
    web_hotel: WebHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#415: гости в Wi-Fi отеля делят один адрес — бюджет тратит только отказ
    (канон входа персонала, #207). Повторные входы тем же листком (браузер уже
    с cookie чата) — тоже успехи и бюджета не трогают."""
    fake_limits = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_limits)
    monkeypatch.setenv("GUEST_BIND_LINK_CONSUME_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        token = await _issue_token(web_hotel)
        statuses = [(await client.post(f"{BASE}/{token}/session")).status_code for _ in range(5)]
        assert statuses == [200, 200, 200, 200, 200]

        # Отказы тратят: два мусорных токена — и стена даже исправной ссылке.
        for _ in range(2):
            rejected = await client.post(f"{BASE}/{uuid.uuid4().hex}/session")
            assert rejected.status_code == 403
        walled = await client.post(f"{BASE}/{token}/session")
        assert walled.status_code == 429
        assert walled.json()["error"]["code"] == "ERR-WEB-005"
    finally:
        get_settings.cache_clear()

"""Дверь входа и знакомое устройство (spec 0033 §3.3, issue #402).

Сценарий issue на HTTP-слое: гость из Wi-Fi отеля (тот же адрес, что у стойки)
запирает вход по логину с бейджа и весь адрес отеля — браузер стойки, где
сотрудник уже входил, входит; гость видит новый текст отказа.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hospitality.shared.config import get_settings
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD

HOTEL_LOGIN = f"/staff/{HOTEL_SLUG}/login"
HOTEL_IP = {"CF-Connecting-IP": "203.0.113.20"}
LOCKED_TEXT = (
    "Слишком много неудачных попыток входа. Подождите несколько минут "
    "или войдите с устройства, на котором уже входили."
)


async def test_guest_lockout_spares_reception_browser(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    monkeypatch.setenv("STAFF_LOGIN_IP_RATE_LIMIT_ATTEMPTS", "2")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "127.0.0.1")  # адрес ASGI-транспорта
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    get_settings.cache_clear()
    try:
        reception = {"login": portal_hotel.login, "password": PASSWORD}
        assert (await client.post(HOTEL_LOGIN, data=reception, headers=HOTEL_IP)).status_code == 303
        await client.post("/staff/logout")  # конец смены: метка устройства остаётся

        guess = {"login": portal_hotel.login, "password": "guess-password!"}
        for _ in range(2):
            response = await second_client.post(HOTEL_LOGIN, data=guess, headers=HOTEL_IP)
            assert response.status_code == 401
        locked = await second_client.post(HOTEL_LOGIN, data=reception, headers=HOTEL_IP)
        assert locked.status_code == 429
        assert LOCKED_TEXT in locked.text

        back = await client.post(HOTEL_LOGIN, data=reception, headers=HOTEL_IP)
        assert back.status_code == 303
        assert back.headers["location"] == f"/staff/{HOTEL_SLUG}"
    finally:
        get_settings.cache_clear()

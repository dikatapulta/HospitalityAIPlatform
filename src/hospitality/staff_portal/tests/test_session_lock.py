"""Кабинет на общем компьютере: «Запомнить меня» и запертый кабинет (spec 0073).

Браузерная сторона: галочка формы входа, запертая сессия глазами опроса сигнала,
страницы и действия, кнопка «Продолжить работу». Фоновое чтение отличает
заголовок `Sec-Fetch-Mode` — httpx его не шлёт, тесты ставят его сами.
"""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select

from hospitality.platform.models import StaffRole, StaffSession, Tenant
from hospitality.platform.staff_invites import create_invite
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD

SIGNAL = f"/staff/{HOTEL_SLUG}/api/requests/signal"
KEEPALIVE = f"/staff/{HOTEL_SLUG}/api/session/keepalive"
BACKGROUND = {"sec-fetch-mode": "cors"}
NAVIGATE = {"sec-fetch-mode": "navigate"}
SAME_ORIGIN_JSON = {"origin": "https://test", "content-type": "application/json"}


async def _sessions() -> list[StaffSession]:
    async with platform_session_scope() as session:
        return list((await session.scalars(select(StaffSession))).all())


async def _idle_for(seconds: float) -> None:
    async with platform_session_scope() as session:
        for row in (await session.scalars(select(StaffSession))).all():
            row.last_used_at = utc_now() - timedelta(seconds=seconds)


def _lock_seconds() -> int:
    return get_settings().staff_session_short_idle_minutes * 60


async def test_login_form_offers_remember_checkbox_unchecked(
    client: AsyncClient, canonical_database: None
) -> None:
    async with platform_session_scope() as session:
        session.add(Tenant(slug="form-hotel", name="Form Hotel"))
    html = (await client.get("/staff/form-hotel/login")).text
    assert 'name="remember" value="1"' in html
    assert "Запомнить меня на этом устройстве" in html
    assert "без галочки кабинет запирается после 15 минут без действий." in html
    assert 'value="1" aria-describedby="remember-hint" checked' not in html


async def test_checkbox_decides_remembered_session(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.post(
        f"/staff/{HOTEL_SLUG}/login",
        data={"login": portal_hotel.login, "password": PASSWORD, "remember": "1"},
    )
    assert response.status_code == 303
    # Второй вход отозвал первый: живая — запомненная.
    live = [row for row in await _sessions() if row.revoked_at is None]
    assert [row.remembered for row in live] == [True]
    revoked = [row for row in await _sessions() if row.revoked_at is not None]
    assert [row.remembered for row in revoked] == [False]

    # Не «1» — короткая: урезанная форма не получает 30 дней.
    await client.post(
        f"/staff/{HOTEL_SLUG}/login",
        data={"login": portal_hotel.login, "password": PASSWORD, "remember": "on"},
    )
    live = [row for row in await _sessions() if row.revoked_at is None]
    assert [row.remembered for row in live] == [False]

    signal = (await client.get(SIGNAL, headers=BACKGROUND)).json()
    assert signal["session"]["locked"] is False


async def test_failed_login_keeps_checkbox(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.post(
        f"/staff/{HOTEL_SLUG}/login",
        data={"login": portal_hotel.login, "password": "wrong-password", "remember": "1"},
    )
    assert response.status_code == 401
    assert 'aria-describedby="remember-hint" checked' in response.text


async def test_remembered_session_has_no_lock(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await client.post(
        f"/staff/{HOTEL_SLUG}/login",
        data={"login": portal_hotel.login, "password": PASSWORD, "remember": "1"},
    )
    signal = (await client.get(SIGNAL, headers=BACKGROUND)).json()
    assert signal["session"] == {"locked": False, "locks_in": None}


async def test_locked_cabinet_still_rings_but_refuses_pages_and_actions(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    await _idle_for(_lock_seconds() + 60)
    before = (await _sessions())[0].last_used_at

    signal = await client.get(SIGNAL, headers=BACKGROUND)
    assert signal.status_code == 200
    assert signal.json()["session"] == {"locked": True, "locks_in": 0}

    page = await client.get(f"/staff/{HOTEL_SLUG}", headers=NAVIGATE)
    assert page.status_code == 303
    assert page.headers["location"] == f"/staff/{HOTEL_SLUG}/login"
    # Без заголовка — действие человека: запертой не проходит.
    assert (await client.get(SIGNAL)).status_code == 401
    keepalive = await client.post(KEEPALIVE, headers=SAME_ORIGIN_JSON, content="{}")
    assert keepalive.status_code == 401
    assert keepalive.json()["error"]["code"] == "ERR-AUTH-002"
    # Ни опрос, ни отказы не продлили запертую.
    assert (await _sessions())[0].last_used_at == before

    # Отпирает только вход: новая сессия, запертая отозвана.
    await submit_login(client, portal_hotel.login)
    assert (await client.get(f"/staff/{HOTEL_SLUG}", headers=NAVIGATE)).status_code == 200
    assert sum(row.revoked_at is None for row in await _sessions()) == 1


async def test_background_poll_does_not_keep_short_session_alive(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    await _idle_for(10 * 60)
    before = (await _sessions())[0].last_used_at
    signal = (await client.get(SIGNAL, headers=BACKGROUND)).json()
    assert signal["session"]["locked"] is False
    assert 4 * 60 < signal["session"]["locks_in"] <= 5 * 60
    assert (await _sessions())[0].last_used_at == before


async def test_keepalive_extends_short_session(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    await _idle_for(_lock_seconds() - 60)
    response = await client.post(KEEPALIVE, headers=SAME_ORIGIN_JSON, content="{}")
    assert response.status_code == 204
    signal = (await client.get(SIGNAL, headers=BACKGROUND)).json()
    assert signal["session"]["locks_in"] > _lock_seconds() - 5


async def test_keepalive_requires_same_origin_json(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.post(KEEPALIVE, headers={"content-type": "application/json"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-009"


async def test_accepted_invite_logs_in_short(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Ссылку открывают и на общем компьютере (spec 0073 §2) — вход без галочки."""
    grant = await create_invite(
        portal_hotel.tenant_id,
        StaffRole.STAFF,
        "Санжар",
        "SANZ",
        invited_by=portal_hotel.user_id,
    )
    response = await client.post(f"/staff/invite/{grant.invite_token}", data={"password": PASSWORD})
    assert response.status_code == 303
    assert [row.remembered for row in await _sessions()] == [False]

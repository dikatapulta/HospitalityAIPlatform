"""Страница заселения и JSON-действия карточки Stay (spec 0033 §6/§10, PR E).

Смоук страницы (аутентифицированность, роли, форма, карточка) + действия:
заселение с guests_count и выездом «12:00 по поясу отеля → UTC», bind-ссылка
(QR), перевыпуск кода, переселение/продление/выезд, счётчик привязок,
CSRF-контракт JSON-действий.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.conftest import FakeBindLinkRedis
from tests.test_staff_auth import create_staff_user

SAME_ORIGIN = {"origin": "https://test"}
CHECKIN_PAGE = f"/staff/{HOTEL_SLUG}/checkin"
STAYS_API = f"/staff/{HOTEL_SLUG}/api/stays"
HOTEL_ZONE = ZoneInfo("Asia/Almaty")

# Код заселения — 6 цифр в показе «482-913» (Ф-2, spec 0033 §11).
CODE_PATTERN = re.compile(r"\b\d{3}-\d{3}\b")


@pytest.fixture
def bind_redis(monkeypatch: pytest.MonkeyPatch) -> FakeBindLinkRedis:
    fake = FakeBindLinkRedis()
    monkeypatch.setattr("hospitality.modules.guests.bindlink.create_redis_client", lambda: fake)
    return fake


async def _submit_checkin(
    client: AsyncClient, room: str, *, nights: str = "1", guests: str = "1"
) -> httpx.Response:
    return await client.post(CHECKIN_PAGE, data={"room": room, "nights": nights, "guests": guests})


async def _checked_in_stay(client: AsyncClient, portal_hotel: PortalHotel) -> guests_api.StayRead:
    """Заселить комнату 101 через страницу и вернуть Stay из БД."""
    response = await _submit_checkin(client, "101")
    assert response.status_code == 200
    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.find_active_stay("101")
    assert stay is not None
    return stay


async def test_checkin_requires_session_and_role(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Без сессии — логин; роль staff — «нет доступа» (мини-матрица §3.2)."""
    response = await client.get(CHECKIN_PAGE)
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"

    email = f"staff-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(client, email)
    forbidden = await client.get(CHECKIN_PAGE)
    assert forbidden.status_code == 403
    assert "Нет доступа" in forbidden.text


async def test_checkin_form_renders_for_receptionist(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    email = f"reception-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST)
    await submit_login(client, email)
    response = await client.get(CHECKIN_PAGE)
    assert response.status_code == 200
    assert "Заселить" in response.text
    assert 'name="room"' in response.text
    assert 'name="nights"' in response.text
    assert "3+" in response.text  # кнопка гостей «3+»
    assert "/staff/static/checkin.js" in response.text


async def test_checkin_creates_stay_with_code_qr_and_hotel_noon(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    """Заселение: код цифрами один раз, QR bind-ссылки, guests_count и
    check_out = заезд + ночи, 12:00 по поясу отеля → UTC (Q3)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    response = await _submit_checkin(client, "305", nights="2", guests="3")
    assert response.status_code == 200
    assert CODE_PATTERN.search(response.text), "код заселения не показан"
    assert "<svg" in response.text  # QR серверным SVG (segno)
    # Белая подложка внутри картинки: без неё QR не сканируется в тёмной теме.
    assert 'fill="#fff"' in response.text
    assert "Выселить" in response.text
    assert len(bind_redis.values) == 1  # bind-ссылка выпущена

    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.find_active_stay("305")
    assert stay is not None
    assert stay.guests_count == 3
    expected_local = datetime.now(tz=HOTEL_ZONE).date() + timedelta(days=2)
    assert stay.check_out_at == datetime.combine(
        expected_local, time(12), tzinfo=HOTEL_ZONE
    ).astimezone(UTC)


async def test_checkin_occupied_room_shows_existing_card(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    """Повторный submit (refresh) — не дубль: карточка занятой комнаты."""
    await submit_login(client, portal_hotel.email)
    await _checked_in_stay(client, portal_hotel)
    repeat = await _submit_checkin(client, "101")
    assert repeat.status_code == 409
    assert "уже заселена" in repeat.text
    assert "Выселить" in repeat.text  # карточка, а не форма
    with tenant_context(portal_hotel.tenant_id):
        stays = await guests_api.list_active_stays()
    assert len(stays) == 1


async def test_checkin_search_finds_card_or_offers_form(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    await submit_login(client, portal_hotel.email)
    await _checked_in_stay(client, portal_hotel)

    occupied = await client.get(CHECKIN_PAGE, params={"room": "101"})
    assert occupied.status_code == 200
    assert "Комната 101" in occupied.text
    assert "Выселить" in occupied.text

    free = await client.get(CHECKIN_PAGE, params={"room": "999"})
    assert free.status_code == 200
    assert "свободна" in free.text
    assert "Заселить" in free.text


async def test_bind_link_action_returns_qr_and_respects_csrf(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    await submit_login(client, portal_hotel.email)
    stay = await _checked_in_stay(client, portal_hotel)

    issued = await client.post(f"{STAYS_API}/{stay.id}/bind-link", json={}, headers=SAME_ORIGIN)
    assert issued.status_code == 200
    body = issued.json()
    assert f"/w/{HOTEL_SLUG}/b/" in body["bind_url"]
    assert body["qr_svg"].startswith("<svg")
    assert 'fill="#fff"' in body["qr_svg"]  # подложка едет и в перевыпущенном QR
    assert body["expires_in_seconds"] == guests_api.BIND_LINK_TTL_SECONDS

    # CSRF-щит: без Origin (канон ERR-AUTH-009).
    rejected = await client.post(f"{STAYS_API}/{stay.id}/bind-link", json={})
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "ERR-AUTH-009"


async def test_stay_actions_move_extend_checkout_and_bindings(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    stay = await _checked_in_stay(client, portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number="102", check_out_at=stay.check_out_at + timedelta(days=1)
            )
        )

    # Переселение в занятую — дружелюбный 409 кодом домена.
    conflict = await client.post(
        f"{STAYS_API}/{stay.id}/move", json={"room_number": "102"}, headers=SAME_ORIGIN
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "ERR-GUESTS-002"

    moved = await client.post(
        f"{STAYS_API}/{stay.id}/move", json={"room_number": "205"}, headers=SAME_ORIGIN
    )
    assert moved.status_code == 200
    assert moved.json()["room_number"] == "205"

    # Продление на 1 ночь — дата в ответе локальная и сдвинута на день.
    before_local = stay.check_out_at.astimezone(HOTEL_ZONE)
    extended = await client.post(
        f"{STAYS_API}/{stay.id}/extend", json={"nights": 1}, headers=SAME_ORIGIN
    )
    assert extended.status_code == 200
    assert extended.json()["check_out_local"] == (before_local + timedelta(days=1)).strftime(
        "%d.%m.%Y %H:%M"
    )

    # Счётчик привязок: 0 → привязка по bind-пути → 1.
    zero = await client.get(f"{STAYS_API}/{stay.id}/bindings")
    assert zero.status_code == 200
    assert zero.json()["count"] == 0
    with tenant_context(portal_hotel.tenant_id):
        grant = await guests_api.start_guest_session_for_stay(
            guests_api.GuestSessionBind(
                stay_id=stay.id,
                identity_external_id=str(uuid.uuid4()),
                consent_version="v1",
            )
        )
    assert grant is not None
    one = await client.get(f"{STAYS_API}/{stay.id}/bindings")
    assert one.json()["count"] == 1

    closed = await client.post(f"{STAYS_API}/{stay.id}/checkout", json={}, headers=SAME_ORIGIN)
    assert closed.status_code == 200
    with tenant_context(portal_hotel.tenant_id):
        assert await guests_api.find_active_stay("205") is None


async def test_stay_actions_forbidden_for_staff_role(
    client: AsyncClient, portal_hotel: PortalHotel, bind_redis: FakeBindLinkRedis
) -> None:
    """Мини-матрица §3.2: роль staff не заселяет и не трогает Stay."""
    await submit_login(client, portal_hotel.email)
    stay = await _checked_in_stay(client, portal_hotel)

    email = f"staff-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(client, email)
    actions: tuple[tuple[str, dict[str, str]], ...] = (("bind-link", {}), ("checkout", {}))
    for action, payload in actions:
        response = await client.post(
            f"{STAYS_API}/{stay.id}/{action}", json=payload, headers=SAME_ORIGIN
        )
        assert response.status_code == 403, action
        assert response.json()["error"]["code"] == "ERR-AUTH-003", action
    bindings = await client.get(f"{STAYS_API}/{stay.id}/bindings")
    assert bindings.status_code == 403

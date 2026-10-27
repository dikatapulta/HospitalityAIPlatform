"""Сигнал кабинета (spec 0039 §3, §10; закрывает #381).

Эндпоинт `GET …/api/requests/signal`: только `new`, без текста гостя,
«просрочена» совпадает с меткой очереди, чужой тенант не виден, без сессии —
401 конвертом (signal.js показывает на нём плашку «Вы вышли»). Каркас: каждая
страница отеля подключает `signal.js` с адресом сигнала, страницы входа,
приглашения и «нет доступа» — нет. Поведение самого `signal.js` (звук, повтор,
уведомления) модульных тестов не имеет — проверка руками на staging (§10).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import queue, signal
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_queue import _make_request, _post_action

SIGNAL = f"/staff/{HOTEL_SLUG}/api/requests/signal"
SIGNAL_SCRIPT = "/staff/static/signal.js"


@pytest.fixture
def demo_orders_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Демо-страница тоже страница отеля — канон фикстуры `test_demo_orders`."""
    monkeypatch.setenv("ENABLE_DEMO_ORDERS", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Эндпоинт сигнала
# ---------------------------------------------------------------------------


async def test_signal_lists_only_unclaimed_and_carries_no_guest_text(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Только `new`: взятая и закрытая уже не зовут. Текста гостя нет ни в
    полях, ни где-либо в ответе — уведомление читает любой у экрана."""
    waiting = await _make_request(portal_hotel.tenant_id, "SECRET-GUEST-TEXT", room_number="305")
    claimed = await _make_request(portal_hotel.tenant_id, "взятая", room_number="401")
    done = await _make_request(portal_hotel.tenant_id, "закрытая", room_number="502")
    await submit_login(client, portal_hotel.login)
    assert (await _post_action(client, claimed.id, "claim")).status_code == 200
    assert (await _post_action(client, done.id, "claim")).status_code == 200
    assert (await _post_action(client, done.id, "complete")).status_code == 200

    response = await client.get(SIGNAL)

    assert response.status_code == 200
    unclaimed = response.json()["unclaimed"]
    assert unclaimed == [
        {
            "id": str(waiting.id),
            "daily_number": waiting.daily_number,
            "room_number": "305",
            "category_name": "Уборка",
            "is_urgent": False,
            "is_overdue": False,
            "age_label": "только что",
            "is_raised": False,
        }
    ]
    assert "SECRET-GUEST-TEXT" not in response.text


async def test_signal_marks_urgent_and_request_without_room(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Срочная — флагом (по нему повтор каждые 20 с и «🚨» во вкладке); без
    комнаты — `null`, и уведомление пишет одну службу."""
    await _make_request(portal_hotel.tenant_id, "течёт вода", room_number=None, is_urgent=True)
    await submit_login(client, portal_hotel.login)

    [item] = (await client.get(SIGNAL)).json()["unclaimed"]

    assert item["is_urgent"] is True
    assert item["room_number"] is None


async def test_signal_overdue_matches_queue_mark(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Просрочена» — тем же сроком отеля, что метка в очереди (spec 0028):
    пер-категорийный срок `maintenance` 10 минут, базовый 240 — на 31-й минуте
    звать о просрочке должна ровно труба, и очередь подсвечивает ровно её."""
    await store_hotel_config(
        portal_hotel.tenant_id,
        reminder_after_minutes=240,
        reminder_minutes_by_category={"maintenance": 10},
    )
    towels = await _make_request(portal_hotel.tenant_id, "убрать 305")
    pipe = await _make_request(
        portal_hotel.tenant_id,
        "течёт труба",
        category_key="maintenance",
        category_name="Техника",
        room_number="101",
    )
    await submit_login(client, portal_hotel.login)
    later = utc_now() + timedelta(minutes=31)
    monkeypatch.setattr(signal, "utc_now", lambda: later)
    monkeypatch.setattr(queue, "utc_now", lambda: later)

    unclaimed = {item["id"]: item for item in (await client.get(SIGNAL)).json()["unclaimed"]}
    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")

    assert unclaimed[str(pipe.id)]["is_overdue"] is True
    assert unclaimed[str(pipe.id)]["age_label"] == "31 мин"
    assert unclaimed[str(towels.id)]["is_overdue"] is False
    assert page.text.count("просрочена") == 1


async def test_signal_does_not_show_other_hotel(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Заявка чужого отеля в сигнал не попадает (RLS), а чужой slug — 403:
    роль доказывает право только в своём отеле."""
    async with platform_session_scope() as session:
        alien = Tenant(slug="hotel-alien", name="Alien Hotel")
        session.add(alien)
        await session.flush()
        alien_id = alien.id
    await _make_request(alien_id, "чужая заявка", room_number="999")
    own = await _make_request(portal_hotel.tenant_id, "своя заявка")
    await submit_login(client, portal_hotel.login)

    response = await client.get(SIGNAL)
    foreign = await client.get("/staff/hotel-alien/api/requests/signal")

    assert [item["id"] for item in response.json()["unclaimed"]] == [str(own.id)]
    assert foreign.status_code == 403


async def test_signal_without_session_is_401_envelope(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """JSON-исход, не редирект на вход: signal.js на 401 показывает плашку
    «Вы вышли из кабинета» и остаётся на странице (§3.3)."""
    response = await client.get(SIGNAL)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR-AUTH-002"


async def test_signal_of_empty_hotel_is_empty_list(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get(SIGNAL)
    assert response.status_code == 200
    assert response.json() == {"unclaimed": [], "turnovers": []}


# ---------------------------------------------------------------------------
# Каркас: сигнал на каждой странице отеля
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["", "/requests", "/requests/new", "/checkin", "/summary", "/knowledge", "/team", "/orders"],
)
async def test_every_hotel_page_includes_signal(
    client: AsyncClient, portal_hotel: PortalHotel, demo_orders_enabled: None, path: str
) -> None:
    """ЧП может прийти, пока ресепшен заселяет гостя (§3.1): сигнал — на каждой."""
    await submit_login(client, portal_hotel.login)
    response = await client.get(f"/staff/{HOTEL_SLUG}{path}")
    assert response.status_code == 200, path
    assert SIGNAL_SCRIPT in response.text, path
    assert f'data-url="{SIGNAL}"' in response.text, path
    assert "Включить звук" in response.text, path


async def test_checkin_form_outcomes_include_signal(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """POST «Заселить» рендерит страницу сам (успех, 422, 409) — и тоже с сигналом."""
    await submit_login(client, portal_hotel.login)
    checkin = f"/staff/{HOTEL_SLUG}/checkin"
    origin = {"origin": "https://test"}

    created = await client.post(checkin, data={"room": "305", "nights": "1"}, headers=origin)
    occupied = await client.post(checkin, data={"room": "305", "nights": "1"}, headers=origin)
    invalid = await client.post(checkin, data={"room": "", "nights": "1"}, headers=origin)

    for response, status in ((created, 200), (occupied, 409), (invalid, 422)):
        assert response.status_code == status
        assert SIGNAL_SCRIPT in response.text, status


async def test_pages_without_session_or_membership_have_no_signal(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Вход, приглашение и «нет доступа» сигнал не подключают: опрашивать его
    там нечем — сессии или членства нет, и он сразу показал бы «Вы вышли»."""
    anonymous = [
        await client.get("/staff/login"),
        await client.get(f"/staff/{HOTEL_SLUG}/login"),
        await client.get("/staff/invite/no-such-token"),
    ]
    async with platform_session_scope() as session:
        session.add(Tenant(slug="hotel-alien", name="Alien Hotel"))
    await submit_login(client, portal_hotel.login)
    forbidden = await client.get("/staff/hotel-alien")

    for response in [*anonymous, forbidden]:
        assert SIGNAL_SCRIPT not in response.text, response.url
        assert "data-signal-banner" not in response.text, response.url
    assert forbidden.status_code == 403


async def test_signal_script_is_served(client: AsyncClient) -> None:
    response = await client.get(SIGNAL_SCRIPT)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


async def test_unclaimed_list_is_newest_first(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Новые сверху — порядок `list_unclaimed_requests`: при потолке в 50 в
    срез попадают свежие, о которых ещё не звали."""
    first = await _make_request(portal_hotel.tenant_id, "первая")
    second = await _make_request(portal_hotel.tenant_id, "вторая")
    await submit_login(client, portal_hotel.login)

    ids = [item["id"] for item in (await client.get(SIGNAL)).json()["unclaimed"]]

    assert ids == [str(second.id), str(first.id)]
    with tenant_context(portal_hotel.tenant_id):
        stored = await requests_api.get_request(first.id)
    assert stored.status is requests_api.RequestStatus.NEW  # GET ничего не меняет

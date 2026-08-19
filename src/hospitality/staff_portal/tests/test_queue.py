"""Очередь заявок кабинета (spec 0033 §5/§10, PR D серии #48; закрывает #56).

Смоук страницы и fragment-эндпоинта (аутентифицированность, карточки, фильтры,
вкладка «закрытые за сегодня») + JSON-действия «взять»/«готово»/«отменить»:
claimed_by пишется, повторное «взять» — 409 ERR-REQUESTS-003, CSRF-контракт
(Content-Type: application/json + непустой same-origin Origin) держит.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import queue
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)

SAME_ORIGIN = {"origin": "https://test"}


async def _make_request(
    tenant_id: uuid.UUID,
    summary: str = "убрать 305",
    *,
    category_key: str = "housekeeping",
    category_name: str = "Уборка",
    room_number: str | None = "305",
    is_urgent: bool = False,
) -> requests_api.ServiceRequestRead:
    """Категория (если ещё нет) + заявка от имени тенанта — шаг почти каждого теста."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get(category_key)
        if category is None:
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=category_key, name=category_name)
            )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                summary=summary,
                room_number=room_number,
                is_urgent=is_urgent,
            )
        )


def _action_url(request_id: uuid.UUID, action: str) -> str:
    return f"/staff/{HOTEL_SLUG}/api/requests/{request_id}/{action}"


async def _post_action(
    client: AsyncClient, request_id: uuid.UUID, action: str, payload: dict[str, str] | None = None
) -> httpx.Response:
    """POST действия по CSRF-контракту: JSON-тело (`{}` без примечания) + Origin."""
    return await client.post(
        _action_url(request_id, action), json=payload or {}, headers=SAME_ORIGIN
    )


# ---------------------------------------------------------------------------
# Страница и fragment (смоук §10)
# ---------------------------------------------------------------------------


async def test_queue_page_renders_cards(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert response.status_code == 200
    assert f"#{request.daily_number}" in response.text
    assert "убрать 305" in response.text
    assert "Комната 305" in response.text
    assert "Уборка" in response.text  # категория на карточке
    assert "Взять" in response.text
    assert "Закрытые за сегодня" in response.text
    assert "Мои" in response.text
    assert "/staff/static/queue.js" in response.text  # поллинг подключён


async def test_urgent_request_is_marked_in_the_queue(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Срочная заявка помечена и в кабинете, не только в чате (spec 0034 §5).

    Бейдж — рядом с бейджем состояния, а не вместо него: «срочная и
    просроченная» обязана читаться целиком.
    """
    await _make_request(portal_hotel.tenant_id, "течёт вода с потолка", is_urgent=True)
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert response.status_code == 200
    assert "🚨 срочно" in response.text
    assert "request-card-urgent" in response.text


async def test_ordinary_request_has_no_urgency_mark(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "🚨 срочно" not in response.text


async def test_queue_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    for path in (f"/staff/{HOTEL_SLUG}/requests", f"/staff/{HOTEL_SLUG}/requests/fragment"):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/staff/login", path


async def test_queue_fragment_returns_list_without_layout(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}/requests/fragment")
    assert response.status_code == 200
    assert "убрать 305" in response.text
    assert "<html" not in response.text  # фрагмент — без каркаса страницы


async def test_queue_category_filter_chips(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await _make_request(
        portal_hotel.tenant_id,
        "починить кран",
        category_key="maintenance",
        category_name="Ремонт",
        room_number="210",
    )
    await submit_login(client, portal_hotel.email)

    filtered = await client.get(f"/staff/{HOTEL_SLUG}/requests", params={"category": "maintenance"})
    assert filtered.status_code == 200
    assert "починить кран" in filtered.text
    assert "убрать 305" not in filtered.text


async def test_queue_mine_filter(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """«Мои» — фильтр по claimed_by текущего сотрудника, не замок (§5)."""
    mine = await _make_request(portal_hotel.tenant_id, "моя заявка")
    await _make_request(portal_hotel.tenant_id, "ничья заявка")
    await submit_login(client, portal_hotel.email)
    assert (await _post_action(client, mine.id, "claim")).status_code == 200

    response = await client.get(f"/staff/{HOTEL_SLUG}/requests", params={"mine": "1"})
    assert response.status_code == 200
    assert "моя заявка" in response.text
    assert "ничья заявка" not in response.text


async def test_closed_today_tab(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Вкладка «закрытые за сегодня»: закрытая сейчас видна, открытая — нет."""
    done = await _make_request(portal_hotel.tenant_id, "выполненная")
    await _make_request(portal_hotel.tenant_id, "открытая")
    await submit_login(client, portal_hotel.email)
    assert (await _post_action(client, done.id, "claim")).status_code == 200
    assert (
        await _post_action(client, done.id, "complete", {"note": "готово не всё"})
    ).status_code == 200

    closed_tab = await client.get(f"/staff/{HOTEL_SLUG}/requests", params={"tab": "closed"})
    assert closed_tab.status_code == 200
    assert "выполненная" in closed_tab.text
    assert "готово не всё" in closed_tab.text  # примечание закрытия на карточке
    assert "открытая" not in closed_tab.text

    open_tab = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "выполненная" not in open_tab.text
    assert "открытая" in open_tab.text


async def test_overdue_request_is_marked_by_tenant_deadline(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не взяли дольше срока отеля — карточка помечена «просрочена» (spec 0028).

    Срок берётся из конфига тенанта тем же `reminder_delay_for`, что и у
    напоминаний воркера: подсветка в кабинете и напоминание в чат службы обязаны
    говорить об одной заявке. Часы двигаем вперёд, а не подделываем `created_at`:
    в проде стареет именно заявка, и `_is_overdue` смотрит ровно на эту разницу.
    """
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.email)

    fresh = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "просрочена" not in fresh.text
    assert "request-card-overdue" not in fresh.text

    monkeypatch.setattr(queue, "utc_now", lambda: utc_now() + timedelta(minutes=31))
    overdue = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "просрочена" in overdue.text
    assert "request-card-overdue" in overdue.text
    assert "badge-overdue" in overdue.text


async def test_overdue_mark_follows_category_deadline_and_status(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пер-категорийный срок переопределяет базовый, а взятая заявка не просрочена.

    `maintenance` — 10 минут, базовый — 240: на 31-й минуте просрочен ровно
    прорыв трубы, а уборка ещё нет. Взятая заявка не просрочена никогда
    (spec 0028 §1: «невзятая» — это ровно `new`; висящий `in_progress` — #120).
    """
    await store_hotel_config(
        portal_hotel.tenant_id,
        reminder_after_minutes=240,
        reminder_minutes_by_category={"maintenance": 10},
    )
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    pipe = await _make_request(
        portal_hotel.tenant_id,
        "течёт труба",
        category_key="maintenance",
        category_name="Техника",
        room_number="101",
    )
    await submit_login(client, portal_hotel.email)

    monkeypatch.setattr(queue, "utc_now", lambda: utc_now() + timedelta(minutes=31))
    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert page.text.count("просрочена") == 1  # только maintenance

    assert (await _post_action(client, pipe.id, "claim")).status_code == 200
    claimed = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "просрочена" not in claimed.text


async def test_queue_opens_for_tenant_without_config(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Онбординг не завершён — очередь открывается, просрочек просто нет.

    Конфига у `portal_hotel` нет вовсе: страница обязана работать (деградация
    `_tenant_config` в None), иначе первый же вход в новом отеле упрётся в 500.
    """
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.email)

    monkeypatch.setattr(queue, "utc_now", lambda: utc_now() + timedelta(days=3))
    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert page.status_code == 200
    assert "убрать 305" in page.text
    assert "просрочена" not in page.text


# ---------------------------------------------------------------------------
# JSON-действия (spec 0033 §5)
# ---------------------------------------------------------------------------


async def test_claim_writes_claimed_by_and_page_shows_it(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    response = await _post_action(client, request.id, "claim")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "in_progress"
    assert payload["claimed_by_user_id"] == str(portal_hotel.user_id)
    assert payload["claimed_by_display_name"] == "Аружан Менеджер"

    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "Взял: Аружан Менеджер" in page.text


async def test_repeated_claim_conflicts_with_409(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Повторное «взять» → 409 ERR-REQUESTS-003 (страница показывает «уже взята»)."""
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    assert (await _post_action(client, request.id, "claim")).status_code == 200

    second = await _post_action(client, request.id, "claim")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-REQUESTS-003"


async def test_complete_note_is_optional(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    assert (await _post_action(client, request.id, "claim")).status_code == 200

    response = await _post_action(client, request.id, "complete")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["resolution_note"] is None


async def test_cancel_requires_note(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    for payload in ({}, {"note": "   "}):
        rejected = await client.post(
            _action_url(request.id, "cancel"), json=payload, headers=SAME_ORIGIN
        )
        assert rejected.status_code == 422, payload
        assert rejected.json()["error"]["code"] == "ERR-PLATFORM-002"

    cancelled = await _post_action(client, request.id, "cancel", {"note": "гость передумал"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["resolution_note"] == "гость передумал"


async def test_actions_enforce_csrf_contract(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """CSRF-контракт JSON-действий (README): нет Origin → 403, чужой Origin → 403,
    не-JSON тело → до обработчика не доходит; заявка остаётся нетронутой."""
    request = await _make_request(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    without_origin = await client.post(_action_url(request.id, "claim"), json={})
    assert without_origin.status_code == 403
    assert without_origin.json()["error"]["code"] == "ERR-AUTH-009"

    cross_origin = await client.post(
        _action_url(request.id, "claim"), json={}, headers={"origin": "https://evil.example"}
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json()["error"]["code"] == "ERR-AUTH-009"

    as_form = await client.post(
        _action_url(request.id, "cancel"), data={"note": "x"}, headers=SAME_ORIGIN
    )
    assert as_form.status_code in (403, 422)  # форма не проходит CSRF-контракт

    with tenant_context(portal_hotel.tenant_id):
        stored = await requests_api.get_request(request.id)
    assert stored.status is requests_api.RequestStatus.NEW


async def test_action_without_session_is_401_envelope(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """JSON-действия — JSON-исходы (require_role напрямую), не редиректы страниц."""
    request = await _make_request(portal_hotel.tenant_id)
    response = await client.post(_action_url(request.id, "claim"), json={}, headers=SAME_ORIGIN)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR-AUTH-002"

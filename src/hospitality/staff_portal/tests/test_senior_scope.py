"""Старший службы: сводка дня и журнал своих служб (spec 0045 §3, §10 «PR 3»).

Граница держится на сервере: старший чужой службы не видит ни строк, ни чисел
другой службы — ни на страницах, ни в JSON сигнала. Эскалаций у старшего нет.
Ресепшен любого уровня и менеджер видят весь отель; сотрудник — ни сводки, ни
журнала. Метки `PIPE-…` — текст заявки техники: по ним видно, что чужое не
просочилось ни в одно место.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffAction, StaffRole
from hospitality.platform.staff_actions import StaffActionRecord, StaffActor
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_changes import _seed
from hospitality.staff_portal.tests.test_queue import _make_request
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
SUMMARY = f"{HOME}/summary"
CHANGES = f"{HOME}/changes"
SIGNAL = f"{HOME}/api/requests/signal"
QUEUE = f"{HOME}/requests"
ESCALATION_TILE = "Бот звал сотрудника"


def _tile(html: str, label: str) -> str:
    match = re.search(
        rf'<span class="tile-label">{label}</span>\s*<span class="tile-value">([^<]*)</span>',
        html,
    )
    assert match is not None, label
    return match.group(1)


def _numbers(html: str) -> tuple[str, str, str]:
    return _tile(html, "Создано"), _tile(html, "Закрыто"), _tile(html, "Открыто сейчас")


async def _login(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole, *service_keys: str
) -> str:
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=service_keys
    )
    await submit_login(client, staff_login)
    return staff_login


async def _hotel_day(portal_hotel: PortalHotel) -> None:
    """Уборка: открытая и выполненная; техника: три, одна отменена; прачечная: одна."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(portal_hotel.tenant_id, "TOWELS-305")
    done = await _make_request(portal_hotel.tenant_id, "BED-306")
    pipes = [
        await _make_request(
            portal_hotel.tenant_id,
            f"PIPE-{index}",
            category_key="maintenance",
            category_name="Техника",
        )
        for index in range(3)
    ]
    await _make_request(
        portal_hotel.tenant_id, "SHIRT-307", category_key="laundry", category_name="Прачечная"
    )
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.change_request_status(done.id, requests_api.RequestStatus.IN_PROGRESS)
        await requests_api.change_request_status(done.id, requests_api.RequestStatus.DONE)
        await requests_api.change_request_status(pipes[0].id, requests_api.RequestStatus.CANCELLED)


# ---------------------------------------------------------------------------
# Сводка дня
# ---------------------------------------------------------------------------


async def test_senior_summary_shows_only_own_services_without_escalations(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel_day(portal_hotel)
    await _login(client, portal_hotel, StaffRole.SENIOR, "housekeeping")

    response = await client.get(SUMMARY)

    assert response.status_code == 200
    page = response.text
    assert _numbers(page) == ("2", "1", "1")
    assert "Только ваши службы: Уборка." in page
    assert "Техника" not in page and "Прачечная" not in page
    assert ESCALATION_TILE not in page


async def test_senior_of_two_services_sees_both(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel_day(portal_hotel)
    await _login(client, portal_hotel, StaffRole.SENIOR, "housekeeping", "laundry")

    page = (await client.get(SUMMARY)).text

    assert _numbers(page) == ("3", "1", "2")
    assert "Только ваши службы: Уборка, Прачечная." in page
    assert "Техника" not in page


@pytest.mark.parametrize(
    ("role", "service_keys", "escalations"),
    [
        (StaffRole.MANAGER, (), True),
        (StaffRole.SENIOR, (RECEPTION_SERVICE_KEY,), False),
    ],
)
async def test_manager_and_reception_senior_see_the_whole_hotel(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    escalations: bool,
) -> None:
    """Ресепшен видит все заявки (spec 0045 §2) — и сводку всего отеля, но
    эскалации — только менеджеру."""
    await _hotel_day(portal_hotel)
    await _login(client, portal_hotel, role, *service_keys)

    page = (await client.get(SUMMARY)).text

    assert _numbers(page) == ("6", "2", "4")
    assert "Техника" in page and "Только ваши службы" not in page
    assert (ESCALATION_TILE in page) is escalations


@pytest.mark.parametrize("service_keys", [("housekeeping",), (RECEPTION_SERVICE_KEY,)])
async def test_staff_level_has_neither_summary_nor_journal(
    client: AsyncClient, portal_hotel: PortalHotel, service_keys: tuple[str, ...]
) -> None:
    await _login(client, portal_hotel, StaffRole.STAFF, *service_keys)

    for path in (SUMMARY, CHANGES):
        assert (await client.get(path)).status_code == 403
    home = (await client.get(HOME)).text
    assert f'href="{SUMMARY}"' not in home and f'href="{CHANGES}"' not in home


async def test_senior_menu_and_home_lead_to_summary_and_journal(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login(client, portal_hotel, StaffRole.SENIOR, "housekeeping")

    home = (await client.get(HOME)).text

    assert f'href="{SUMMARY}"' in home and f'href="{CHANGES}"' in home
    assert f'href="{HOME}/team"' not in home and f'href="{HOME}/knowledge"' not in home


# ---------------------------------------------------------------------------
# Журнал изменений
# ---------------------------------------------------------------------------

# 07:00 UTC = полдень по Алматы: далеко от обеих границ суток отеля.
_DAY = date(2026, 10, 20)
_NOON = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)


async def _actor(portal_hotel: PortalHotel) -> StaffActor:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    return StaffActor(user_id=user_id, display_name="S", login=staff_login)


def _request_row(
    action: StaffAction,
    number: int,
    source: requests_api.RequestCategoryRead | None,
    target: requests_api.RequestCategoryRead | None = None,
    *,
    legacy_name: str | None = None,
) -> StaffActionRecord:
    return StaffActionRecord(
        action=action,
        request_id=uuid.uuid4(),
        request_daily_number=number,
        request_service_day=_DAY,
        category_name=source.name if source is not None else legacy_name,
        category_id=source.id if source is not None else None,
        category_name_to=target.name if target is not None else None,
        category_id_to=target.id if target is not None else None,
        room_number="305",
    )


async def test_senior_journal_shows_only_rows_of_own_services(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Видны строки, где служба «откуда» или «куда» — своя (передача — обеим
    службам). Не видны: чужая служба, передача между чужими, заселение и строка
    до миграции 0034 без службы (её видит менеджер). Логины фильтра — только из
    видимых строк, чужой логин в `?login=` строк не открывает."""
    await store_hotel_config(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        hk, mt, ld = [
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
            for key, name in (
                ("housekeeping", "Уборка"),
                ("maintenance", "Техника"),
                ("laundry", "Прачка"),
            )
        ]
    transferred = StaffAction.REQUEST_TRANSFERRED
    rows: list[tuple[StaffActionRecord, bool]] = [
        (_request_row(StaffAction.REQUEST_CLAIMED, 11, hk), True),
        (_request_row(StaffAction.REQUEST_CLAIMED, 12, mt), False),
        (_request_row(transferred, 13, mt, hk), True),
        (_request_row(transferred, 14, hk, mt), True),
        (_request_row(transferred, 15, mt, ld), False),
        (StaffActionRecord(action=StaffAction.STAY_CHECKED_IN, room_number="999"), False),
        (_request_row(StaffAction.REQUEST_DONE, 16, None, legacy_name="Уборка"), False),
    ]
    actors = [await _actor(portal_hotel) for _ in rows]
    for minute, (actor, (record, _)) in enumerate(zip(actors, rows, strict=True)):
        await _seed(portal_hotel.tenant_id, actor, record, _NOON + timedelta(minutes=minute))
    visible = [actor.login for actor, (_, shown) in zip(actors, rows, strict=True) if shown]
    hidden = [actor.login for actor, (_, shown) in zip(actors, rows, strict=True) if not shown]
    await _login(client, portal_hotel, StaffRole.SENIOR, "housekeeping")

    page = (await client.get(CHANGES, params={"day": _DAY.isoformat()})).text

    assert page.count("<tr><td>") == 3
    assert "#11 · Уборка · 305" in page
    assert "#13 · Техника → Уборка · 305" in page
    assert "#14 · Уборка → Техника · 305" in page
    for absent in ("#12", "#15", "#16", "999", "Прачка"):
        assert absent not in page
    assert all(login is not None and login in page for login in visible)
    assert not any(login is not None and login in page for login in hidden)
    assert "Действия с заявками ваших служб: Уборка." in page
    assert "Заселение" not in page  # ни строки, ни пункта фильтра

    foreign_login = actors[1].login
    by_foreign = (
        await client.get(CHANGES, params={"day": _DAY.isoformat(), "login": foreign_login})
    ).text
    assert "<tr><td>" not in by_foreign

    await submit_login(second_client, portal_hotel.login)
    manager_page = (await second_client.get(CHANGES, params={"day": _DAY.isoformat()})).text
    assert manager_page.count("<tr><td>") == len(rows)
    assert "Действия сотрудников с заявками и заселением." in manager_page


async def test_senior_sees_nothing_foreign_on_any_surface(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Сквозная проверка старшего: очередь, главная, сигнал (JSON), сводка и
    журнал — без заявок и действий техники. Действия идут боевым путём кабинета:
    журнал пишет службу сам."""
    await store_hotel_config(portal_hotel.tenant_id)
    towels = await _make_request(portal_hotel.tenant_id, "TOWELS-305")
    pipe, waiting_pipe = [
        await _make_request(
            portal_hotel.tenant_id,
            summary,
            category_key="maintenance",
            category_name="Техника",
            room_number="101",
        )
        for summary in ("PIPE-101", "PIPE-102")
    ]
    await submit_login(client, portal_hotel.login)
    for request in (towels, pipe):
        response = await client.post(
            f"{HOME}/api/requests/{request.id}/claim", json={}, headers={"origin": "https://test"}
        )
        assert response.status_code == 200
    client.cookies.clear()
    await _login(client, portal_hotel, StaffRole.SENIOR, "housekeeping")

    signal = (await client.get(SIGNAL)).json()
    surfaces = {
        "queue": (await client.get(QUEUE)).text,
        "home": (await client.get(HOME)).text,
        "summary": (await client.get(SUMMARY)).text,
        "changes": (await client.get(CHANGES)).text,
        "signal": str(signal),
    }

    for name, body in surfaces.items():
        for marker in ("PIPE-", str(pipe.id), str(waiting_pipe.id)):
            assert marker not in body, (name, marker)
    # Название чужой службы законно есть только в очереди — «Передать» ведёт в любую.
    for name in ("summary", "changes", "signal"):
        assert "Техника" not in surfaces[name], name
    assert signal["unclaimed"] == []  # своя заявка взята, чужая невзятая не звучит
    assert surfaces["changes"].count("<tr><td>") == 1
    assert _numbers(surfaces["summary"]) == ("1", "0", "1")

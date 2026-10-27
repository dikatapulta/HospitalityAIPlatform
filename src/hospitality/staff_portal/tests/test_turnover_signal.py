"""Сигнал кабинета о номерах доски «Выезды и уборка» (spec 0046 §7, §11, PR 2 #476).

Второй список ответа `GET …/api/requests/signal`: хаускипингу — выезд
(`reported`), старшему хаускипинга — убранный номер (`cleaned`), ресепшену —
замечание при проверке, пока не прошли полчаса. Адресаты — по правам доски
`turnovers.rights_for`, менеджера сигнал не зовёт. Выключатель выключен — список
пуст и в базу не ходит. «Один раз» в браузере (звук и уведомление на вкладку,
плашка до «Понятно») — это `signal.js`, модульных тестов JS нет: проверка руками
(канон spec 0039 §10).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import signal
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from hospitality.staff_portal.tests.test_turnovers import (
    HOUSEKEEPING,
    RECEPTION,
    _categories,
    _login_as,
    _seed_path,
)

SIGNAL = f"/staff/{HOTEL_SLUG}/api/requests/signal"
NOTE = "минибар: 2 колы <b>"

Status = housekeeping_api.TurnoverStatus


@pytest.fixture(autouse=True)
def _board_enabled() -> Iterator[None]:
    """Доска включена во всех тестах файла — канон фикстуры `test_turnovers`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _walk(
    portal_hotel: PortalHotel, room: str, steps: tuple[Status, ...], note: str | None = None
) -> uuid.UUID:
    """Путь номера от выезда через `steps`; замечание — на шаге «Проверено»."""
    turnover_id = await _seed_path(portal_hotel, room)
    with tenant_context(portal_hotel.tenant_id):
        for status in steps:
            await housekeeping_api.change_turnover_status(
                turnover_id,
                status,
                inspection_note=note if status is Status.INSPECTED else None,
            )
    return turnover_id


async def _board_of_every_step(portal_hotel: PortalHotel) -> None:
    """701 ждёт проверки, 702 ждёт приёмки, 703 проверен с замечанием, 705 —
    без замечания, 706 уже готов: каждому адресату — своё, готовый — никому."""
    await _categories(portal_hotel)
    await _walk(portal_hotel, "701", ())
    await _walk(portal_hotel, "702", (Status.INSPECTED, Status.CLEANING, Status.CLEANED))
    await _walk(portal_hotel, "703", (Status.INSPECTED,), note=NOTE)
    await _walk(portal_hotel, "705", (Status.INSPECTED,))
    await _walk(
        portal_hotel,
        "706",
        (Status.INSPECTED, Status.CLEANING, Status.CLEANED, Status.READY),
        note="старое",
    )


def _calls(body: dict[str, list[dict[str, str | None]]]) -> set[tuple[str | None, str | None]]:
    return {(item["room_number"], item["kind"]) for item in body["turnovers"]}


@pytest.mark.parametrize(
    ("role", "service_keys", "expected"),
    [
        (StaffRole.STAFF, HOUSEKEEPING, {("701", "inspect")}),
        (StaffRole.SENIOR, HOUSEKEEPING, {("701", "inspect"), ("702", "accept")}),
        (StaffRole.STAFF, RECEPTION, {("703", "remark")}),
        (StaffRole.SENIOR, RECEPTION, {("703", "remark")}),
        # Права на всё у менеджера — подстраховка: сигнал его не зовёт (§7).
        (StaffRole.MANAGER, (), set()),
        (StaffRole.STAFF, ("maintenance",), set()),
        (StaffRole.SENIOR, ("maintenance",), set()),
    ],
)
async def test_turnover_calls_by_status_and_role(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    expected: set[tuple[str, str]],
) -> None:
    await _board_of_every_step(portal_hotel)
    await _login_as(client, portal_hotel, role, service_keys)

    response = await client.get(SIGNAL)

    assert response.status_code == 200
    assert _calls(response.json()) == expected


async def test_remark_carries_note_as_is_and_others_carry_none(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Текст замечания идёт как есть — экранирует его `signal.js` (textContent);
    у «Проверьте» и «Примите» текста нет."""
    await _board_of_every_step(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, (*RECEPTION, *HOUSEKEEPING))

    items = (await client.get(SIGNAL)).json()["turnovers"]

    notes = {(item["room_number"], item["kind"]): item["note"] for item in items}
    assert notes == {("701", "inspect"): None, ("703", "remark"): NOTE}


async def test_remark_calls_reception_only_within_window(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Один раз» на сервере: замечание зовёт полчаса с «Замечание» и не
    возвращается, когда номер идёт дальше по шагам — гость уже ушёл."""
    await _categories(portal_hotel)
    turnover_id = await _walk(portal_hotel, "703", (Status.INSPECTED,), note=NOTE)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    fresh = (await client.get(SIGNAL)).json()["turnovers"]
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.change_turnover_status(turnover_id, Status.CLEANING)
    later = utc_now() + signal.REMARK_SIGNAL_WINDOW + timedelta(minutes=1)
    monkeypatch.setattr(signal, "utc_now", lambda: later)
    expired = (await client.get(SIGNAL)).json()["turnovers"]

    assert [(item["id"], item["kind"]) for item in fresh] == [(str(turnover_id), "remark")]
    assert expired == []


async def test_board_off_signal_has_no_turnovers_and_skips_db(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Выключатель выключен — второй список пуст, и сигнал раз в 15 с с каждой
    вкладки не читает таблицу путей (spec 0046 §5: та же `board_enabled()`)."""
    await _board_of_every_step(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.SENIOR, HOUSEKEEPING)
    monkeypatch.setenv("ENABLE_TURNOVER_BOARD", "false")
    get_settings.cache_clear()

    async def _no_db(**_: object) -> list[housekeeping_api.RoomTurnoverRead]:
        raise AssertionError("signal read room_turnovers with the board off")

    monkeypatch.setattr(housekeeping_api, "list_open_turnovers", _no_db)

    response = await client.get(SIGNAL)

    assert response.status_code == 200
    assert response.json()["turnovers"] == []


async def test_hotel_page_carries_board_url_and_remark_row_template(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Каркас: адрес доски для перехода по уведомлению и строка плашки замечаний
    — `<template>`, который `signal.js` клонирует; сервер текст туда не пишет."""
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    page = (await client.get(f"/staff/{HOTEL_SLUG}")).text

    assert f'data-turnovers-url="/staff/{HOTEL_SLUG}/turnovers"' in page
    assert "<template data-signal-remark-row>" in page
    assert "Понятно" in page

"""Доска «Выезды и уборка» в кабинете (spec 0046 §4–§6, §11, issue #476).

Права каждого шага по уровню и службе (ERR-HOUSEKEEPING-003), меню и 403 доски;
отметка выезда с подтверждением выселения (004) и без хаускипинга в кабинете
(005); «Выселить» на карточке отправляет номер на доску; автовыезд пути не
создаёт; доска рисует группы, замечание и «Сегодня убрано».
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

BOARD = f"/staff/{HOTEL_SLUG}/turnovers"
FRAGMENT = f"{BOARD}/fragment"
ACTIONS = f"/staff/{HOTEL_SLUG}/api/turnovers"
HOME = f"/staff/{HOTEL_SLUG}"
SAME_ORIGIN = {"origin": "https://test"}

RECEPTION = (RECEPTION_SERVICE_KEY,)
HOUSEKEEPING = (HOUSEKEEPING_SERVICE_KEY,)


async def _categories(portal_hotel: PortalHotel) -> None:
    """Службы отеля: ресепшен, уборка и инженерия (ключи профиля пилота)."""
    with tenant_context(portal_hotel.tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            (HOUSEKEEPING_SERVICE_KEY, "Уборка и бельё"),
            ("maintenance", "Инженерная служба"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )


async def _member(
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    *,
    name: str = "Test Staff",
) -> str:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=role,
        service_keys=service_keys,
        display_name=name,
    )
    return staff_login


async def _hotel_with_housekeeping(portal_hotel: PortalHotel) -> None:
    """Отель, где хаускипинг уже в кабинете: категории + одна горничная."""
    await _categories(portal_hotel)
    await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, name="Айгуль")


async def _login_as(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    *,
    name: str = "Test Staff",
) -> None:
    await submit_login(client, await _member(portal_hotel, role, service_keys, name=name))


async def _report(client: AsyncClient, room: str, **extra: object) -> httpx.Response:
    return await client.post(ACTIONS, json={"room_number": room, **extra}, headers=SAME_ORIGIN)


async def _step(
    client: AsyncClient, turnover_id: uuid.UUID, action: str, body: dict[str, str] | None = None
) -> httpx.Response:
    return await client.post(
        f"{ACTIONS}/{turnover_id}/{action}", json=body or {}, headers=SAME_ORIGIN
    )


async def _open_paths(portal_hotel: PortalHotel) -> list[housekeeping_api.RoomTurnoverRead]:
    with tenant_context(portal_hotel.tenant_id):
        return await housekeeping_api.list_open_turnovers(limit=50)


async def _seed_path(portal_hotel: PortalHotel, room: str = "704") -> uuid.UUID:
    with tenant_context(portal_hotel.tenant_id):
        return (await housekeeping_api.report_checkout(room)).turnover.id


# ---------------------------------------------------------------------------
# Кто видит доску
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "service_keys", "sees"),
    [
        (StaffRole.STAFF, RECEPTION, True),
        (StaffRole.STAFF, HOUSEKEEPING, True),
        (StaffRole.SENIOR, HOUSEKEEPING, True),
        (StaffRole.MANAGER, (), True),
        (StaffRole.STAFF, ("maintenance",), False),
        (StaffRole.SENIOR, ("maintenance",), False),
    ],
)
async def test_board_and_menu_by_service(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    sees: bool,
) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    await _login_as(client, portal_hotel, role, service_keys)

    home = await client.get(HOME)
    board = await client.get(BOARD)
    fragment = await client.get(FRAGMENT)

    assert ("Выезды и уборка" in home.text) is sees
    assert board.status_code == (200 if sees else 403)
    assert fragment.status_code == (200 if sees else 403)
    if not sees:
        assert "Нет доступа" in board.text


# ---------------------------------------------------------------------------
# Путь номера целиком
# ---------------------------------------------------------------------------


async def test_reception_reports_and_housekeeping_walks_the_path(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    await _hotel_with_housekeeping(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION, name="Алия")

    first = await _report(client, " 704 ")
    again = await _report(client, "704")
    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["room_number"] == "704"
    assert again.json()["created"] is False
    assert again.json()["since"] == first.json()["since"]
    board = (await client.get(BOARD)).text
    assert "Ждут проверки — 1" in board
    assert "выезд " in board and "· Алия" in board
    assert 'data-action="inspect"' not in board  # ресепшен не проверяет номер
    assert 'data-action="cancel"' in board
    [path] = await _open_paths(portal_hotel)

    await _login_as(second_client, portal_hotel, StaffRole.STAFF, HOUSEKEEPING, name="Айгуль")
    inspected = await _step(second_client, path.id, "inspect", {"note": " Минибар: 2 колы "})
    assert inspected.status_code == 200
    assert inspected.json() == {"room_number": "704", "status": "inspected"}
    assert "⚠ Замечание: Минибар: 2 колы" in (await client.get(FRAGMENT)).text
    assert (await _step(second_client, path.id, "start-cleaning")).status_code == 200
    assert (await _step(second_client, path.id, "finish-cleaning")).status_code == 200
    refused = await _step(second_client, path.id, "accept")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-HOUSEKEEPING-003"

    await _login_as(second_client, portal_hotel, StaffRole.SENIOR, HOUSEKEEPING, name="Гульнара")
    assert "Ждут приёмки — 1" in (await second_client.get(BOARD)).text
    accepted = await _step(second_client, path.id, "accept")
    assert accepted.json()["status"] == "ready"
    senior_board = (await second_client.get(BOARD)).text
    assert "Готовы к заселению сегодня — 1" in senior_board
    assert "готов " in senior_board and "· Гульнара" in senior_board
    assert "Сегодня убрано: 1 · Айгуль — 1" in senior_board
    # Ресепшен видит «готов», но не строку «кто сколько» — она старшему и менеджеру.
    reception_board = (await client.get(BOARD)).text
    assert "Готовы к заселению сегодня — 1" in reception_board
    assert "Сегодня убрано" not in reception_board
    # Шаг вне порядка — конфликт домена, а не 500.
    late = await _step(second_client, path.id, "cancel")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "ERR-HOUSEKEEPING-002"


@pytest.mark.parametrize(
    ("role", "service_keys", "action", "allowed"),
    [
        (StaffRole.STAFF, RECEPTION, "inspect", False),
        (StaffRole.SENIOR, RECEPTION, "accept", False),
        (StaffRole.STAFF, RECEPTION, "cancel", True),
        (StaffRole.STAFF, HOUSEKEEPING, "inspect", True),
        (StaffRole.STAFF, HOUSEKEEPING, "cancel", False),
        (StaffRole.SENIOR, HOUSEKEEPING, "cancel", True),
        (StaffRole.STAFF, ("maintenance",), "inspect", False),
        (StaffRole.MANAGER, (), "inspect", True),
        (StaffRole.MANAGER, (), "cancel", True),
    ],
)
async def test_step_rights_by_level_and_service(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    action: str,
    allowed: bool,
) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    turnover_id = await _seed_path(portal_hotel)
    await _login_as(client, portal_hotel, role, service_keys)

    response = await _step(client, turnover_id, action)

    if allowed:
        assert response.status_code == 200
        return
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-HOUSEKEEPING-003"
    [path] = await _open_paths(portal_hotel)
    assert path.status is housekeeping_api.TurnoverStatus.REPORTED


@pytest.mark.parametrize(
    ("role", "service_keys"),
    [(StaffRole.STAFF, HOUSEKEEPING), (StaffRole.SENIOR, HOUSEKEEPING), (StaffRole.STAFF, ())],
)
async def test_only_reception_and_manager_report_checkout(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    await _login_as(client, portal_hotel, role, service_keys)

    response = await _report(client, "704")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-HOUSEKEEPING-003"
    assert await _open_paths(portal_hotel) == []


async def test_report_requires_csrf_contract(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    await submit_login(client, portal_hotel.login)

    without_origin = await client.post(ACTIONS, json={"room_number": "704"})
    as_form = await client.post(ACTIONS, data={"room_number": "704"}, headers=SAME_ORIGIN)

    assert without_origin.status_code == 403
    assert without_origin.json()["error"]["code"] == "ERR-AUTH-009"
    assert as_form.status_code in (403, 422)  # форма не проходит CSRF-контракт
    assert await _open_paths(portal_hotel) == []


# ---------------------------------------------------------------------------
# Гость в кабинете и выезд с карточки
# ---------------------------------------------------------------------------


async def test_guest_in_room_needs_confirmation_before_check_out(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Опечатка в номере не выключает чат живому гостю молча (spec 0046 §5)."""
    await _hotel_with_housekeeping(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_in(
            guests_api.StayCheckIn(room_number="704", check_out_at=utc_now() + timedelta(days=1))
        )
    await submit_login(client, portal_hotel.login)

    asked = await _report(client, "704")
    assert asked.status_code == 409
    assert asked.json()["error"]["code"] == "ERR-HOUSEKEEPING-004"
    with tenant_context(portal_hotel.tenant_id):
        assert await guests_api.find_active_stay("704") is not None
    assert await _open_paths(portal_hotel) == []

    confirmed = await _report(client, "704", check_out_stay=True)
    assert confirmed.status_code == 200
    assert confirmed.json()["created"] is True
    with tenant_context(portal_hotel.tenant_id):
        assert await guests_api.find_active_stay("704") is None
    assert [path.room_number for path in await _open_paths(portal_hotel)] == ["704"]


async def test_no_housekeeping_in_cabinet_closes_the_form(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Без сотрудников хаускипинга путь никто не увидит — его и не создаём (§5)."""
    await _categories(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="705", check_out_at=utc_now() + timedelta(days=1))
        )
    await submit_login(client, portal_hotel.login)

    board = (await client.get(BOARD)).text
    refused = await _report(client, "704")
    checkout = await client.post(
        f"/staff/{HOTEL_SLUG}/api/stays/{checked.stay.id}/checkout", json={}, headers=SAME_ORIGIN
    )

    assert "В кабинете пока нет сотрудников хаускипинга" in board
    assert 'id="turnover-report"' not in board
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-HOUSEKEEPING-005"
    assert checkout.status_code == 200
    assert checkout.json()["turnover_url"] is None
    assert await _open_paths(portal_hotel) == []


async def test_stay_card_checkout_sends_room_to_board(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="705", check_out_at=utc_now() + timedelta(days=1))
        )
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    response = await client.post(
        f"/staff/{HOTEL_SLUG}/api/stays/{checked.stay.id}/checkout", json={}, headers=SAME_ORIGIN
    )

    assert response.status_code == 200
    assert response.json()["turnover_url"] == BOARD
    assert [path.room_number for path in await _open_paths(portal_hotel)] == ["705"]


async def test_auto_checkout_does_not_create_a_path(portal_hotel: PortalHotel) -> None:
    """Автовыезд забытого Stay — бухгалтерия, а не «гость сдал ключ» (§5)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _hotel_with_housekeeping(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_in(
            guests_api.StayCheckIn(room_number="706", check_out_at=utc_now() - timedelta(hours=5))
        )

    assert await guests_api.close_overdue_stays() == 1
    assert await _open_paths(portal_hotel) == []


async def test_empty_board_says_so(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _hotel_with_housekeeping(portal_hotel)
    await submit_login(client, portal_hotel.login)

    board = (await client.get(BOARD)).text

    assert "Выездов в работе нет." in board
    assert "Сегодня убрано: 0" in board  # менеджеру строка есть и при нуле
    assert 'id="turnover-report"' in board

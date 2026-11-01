"""Этажи хаускипинга в кабинете (spec 0053 §4–§5, §10, issue #478).

Разбор поля «7, 8, 12-14»; страница «Этажи» — выключатель, права, сохранение
и отказы; адресация сигнала: заявка уборки по этажу на первой ступени зовёт
только команду этажа, со второй — всю службу; срочная, этаж без команды и номер
без этажа — всех; строка раздачи отключённой горничной не действует. Выезд
«Проверьте номер» — так же, через срок уборки от отметки.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform import staff_team
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import floors, queue, signal
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_queue import _make_request
from hospitality.staff_portal.tests.test_turnovers import (
    HOUSEKEEPING,
    RECEPTION,
    SAME_ORIGIN,
    _categories,
)
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/floors"
ACTION = f"/staff/{HOTEL_SLUG}/api/floors"
D = timedelta(minutes=30)


@pytest.fixture(autouse=True)
def _floors_enabled() -> Iterator[None]:
    """Этажи и доска включены во всех тестах файла — канон `test_turnovers`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_HOUSEKEEPING_FLOORS", "true")
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


def _switch_floors_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_HOUSEKEEPING_FLOORS", "false")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Чистые функции
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", set()),
        ("  ", set()),
        ("7", {7}),
        ("7, 8", {7, 8}),
        ("7 8;9", {7, 8, 9}),
        ("12-14", {12, 13, 14}),
        ("12 – 14, 3", {3, 12, 13, 14}),
        ("14—12", {12, 13, 14}),
        ("1, 99", {1, 99}),
        ("7,7", {7}),
    ],
)
def test_parse_floors_understands_lists_and_ranges(text: str, expected: set[int]) -> None:
    assert floors.parse_floors(text) == expected


@pytest.mark.parametrize("text", ["7-", "-7", "7-8-9", "седьмой", "7.5", "0", "100", "5-100", "७"])
def test_parse_floors_refuses_garbage_and_out_of_range(text: str) -> None:
    with pytest.raises(ValueError):
        floors.parse_floors(text)


def test_format_floors_folds_runs_of_three() -> None:
    assert floors.format_floors(frozenset({7, 8, 12, 13, 14, 20})) == "7, 8, 12-14, 20"
    assert floors.format_floors(frozenset()) == ""


@pytest.mark.parametrize(
    ("room", "calls_me"),
    [
        ("704", True),  # мой этаж
        ("812", False),  # этаж соседки
        ("1505", True),  # этаж без команды — вся служба
        ("Люкс", True),  # номера без этажа — вся служба
        (None, True),
    ],
)
def test_routing_calls_floor_team_only_on_staffed_floor(room: str | None, calls_me: bool) -> None:
    routing = floors.FloorRouting(staffed_floors=frozenset({7, 8}), my_floors=frozenset({7}))
    assert routing.calls_me_first(room) is calls_me
    assert floors.NO_ROUTING.calls_me_first(room) is True


@pytest.mark.parametrize(
    ("role", "service_keys", "can_view", "can_edit"),
    [
        (StaffRole.STAFF, HOUSEKEEPING, True, False),
        (StaffRole.SENIOR, HOUSEKEEPING, True, True),
        (StaffRole.STAFF, RECEPTION, True, False),
        (StaffRole.SENIOR, RECEPTION, True, False),
        (StaffRole.MANAGER, (), True, True),
        (StaffRole.SENIOR, ("maintenance",), False, False),
    ],
)
def test_rights_by_level_and_service(
    role: StaffRole, service_keys: tuple[str, ...], can_view: bool, can_edit: bool
) -> None:
    audience = StaffAudience(
        tenant_id=uuid.uuid4(), role_key=role, service_keys=frozenset(service_keys)
    )
    assert floors.rights_for(audience) == floors.FloorRights(can_view=can_view, can_edit=can_edit)


# ---------------------------------------------------------------------------
# Страница и сохранение
# ---------------------------------------------------------------------------


async def _member(
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    name: str,
) -> tuple[uuid.UUID, str]:
    login = unique_login()
    user_id = await create_staff_user(
        login,
        tenant_id=portal_hotel.tenant_id,
        role=role,
        service_keys=service_keys,
        display_name=name,
    )
    return user_id, login


async def _save(client: AsyncClient, rows: list[tuple[uuid.UUID, str]]) -> dict[str, object]:
    response = await client.post(
        ACTION,
        json={"rows": [{"user_id": str(user), "floors": text} for user, text in rows]},
        headers=SAME_ORIGIN,
    )
    return {"status": response.status_code, "body": response.json()}


async def test_switched_off_section_is_404_and_out_of_menu(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_floors_off(monkeypatch)
    await submit_login(client, portal_hotel.login)

    page = await client.get(PAGE)
    action = await _save(client, [])
    home = await client.get(f"/staff/{HOTEL_SLUG}")

    assert page.status_code == 404
    assert action["status"] == 404
    assert 'href="/staff/demo-hotel/floors"' not in home.text


@pytest.mark.parametrize(
    ("role", "service_keys", "sees"),
    [
        (StaffRole.STAFF, HOUSEKEEPING, True),
        (StaffRole.STAFF, RECEPTION, True),
        (StaffRole.SENIOR, ("maintenance",), False),
    ],
)
async def test_page_and_menu_by_service(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...],
    sees: bool,
) -> None:
    await _categories(portal_hotel)
    _, login = await _member(portal_hotel, role, service_keys, "Сотрудник")
    await submit_login(client, login)

    page = await client.get(PAGE)
    home = await client.get(f"/staff/{HOTEL_SLUG}")

    assert page.status_code == (200 if sees else 403)
    assert ('href="/staff/demo-hotel/floors"' in home.text) is sees


async def test_senior_saves_distribution_and_page_shows_it(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Старшая раздаёт этажи — «По этажам», время раздачи и строка горничной."""
    await _categories(portal_hotel)
    await store_hotel_config(portal_hotel.tenant_id)
    senior, senior_login = await _member(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING, "Сауле")
    aigul, aigul_login = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Айгуль")
    dina, _ = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Дина")
    await submit_login(client, senior_login)

    before = await client.get(PAGE)
    saved = await _save(client, [(aigul, "7, 8"), (dina, "8-10"), (senior, "")])
    page = await client.get(f"{PAGE}?saved=1")
    await submit_login(second_client, aigul_login)
    maid_page = await second_client.get(PAGE)

    assert floors.NOT_DISTRIBUTED_TEXT in before.text
    assert saved == {"status": 200, "body": {"members": 3}}
    assert "Раздано сегодня в" in page.text
    assert "Сохранено. Раздача действует" in page.text
    assert "<tr><td>8</td><td>Айгуль, Дина</td></tr>" in page.text
    assert 'value="8-10"' in page.text
    assert "Ваши этажи: 7, 8 — заявки и выезды отсюда зовут сначала вас." in maid_page.text
    assert 'id="floors-form"' not in maid_page.text


async def test_maid_and_reception_cannot_save(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _categories(portal_hotel)
    aigul, login = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Айгуль")
    await submit_login(client, login)

    result = await _save(client, [(aigul, "7")])

    assert result["status"] == 403
    assert result["body"]["error"]["code"] == "ERR-HOUSEKEEPING-003"  # type: ignore[index]
    with tenant_context(portal_hotel.tenant_id):
        assert await housekeeping_api.list_floor_assignments() == []


async def test_save_refuses_someone_outside_housekeeping(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Техник или отключённая горничная в раздаче — 409 006, раздача не тронута."""
    await _categories(portal_hotel)
    aigul, _ = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Айгуль")
    tech, _ = await _member(portal_hotel, StaffRole.STAFF, ("maintenance",), "Ержан")
    await submit_login(client, portal_hotel.login)

    result = await _save(client, [(aigul, "7"), (tech, "8")])

    assert result["status"] == 409
    assert result["body"]["error"]["code"] == "ERR-HOUSEKEEPING-006"  # type: ignore[index]
    with tenant_context(portal_hotel.tenant_id):
        assert await housekeeping_api.list_floor_assignments() == []


async def test_save_names_the_row_it_did_not_understand(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Непонятое поле — ошибка проверки запроса с номером строки: по нему
    `floors.js` называет горничную."""
    await _categories(portal_hotel)
    aigul, _ = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Айгуль")
    dina, _ = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Дина")
    await submit_login(client, portal_hotel.login)

    result = await _save(client, [(aigul, "7"), (dina, "7-")])

    error = result["body"]["error"]  # type: ignore[index]
    assert result["status"] == 422
    assert error["code"] == "ERR-PLATFORM-002"
    assert error["details"][0]["loc"][:4] == ["body", "rows", 1, "floors"]


# ---------------------------------------------------------------------------
# Адресация: сигнал
# ---------------------------------------------------------------------------


async def _floor_hotel(portal_hotel: PortalHotel) -> dict[str, uuid.UUID]:
    """Отель со сроком уборки 30 мин: Айгуль — 7-й этаж, Дина — 8-й, у Сауле
    (старшая) и Гульнар этажей нет."""
    await _categories(portal_hotel)
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    people = {
        "aigul": (StaffRole.STAFF, "Айгуль"),
        "dina": (StaffRole.STAFF, "Дина"),
        "gulnar": (StaffRole.STAFF, "Гульнар"),
        "saule": (StaffRole.SENIOR, "Сауле"),
    }
    ids = {
        key: (await _member(portal_hotel, role, HOUSEKEEPING, name))[0]
        for key, (role, name) in people.items()
    }
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.replace_floor_assignments(
            {ids["aigul"]: frozenset({7}), ids["dina"]: frozenset({8})}
        )
    return ids


async def _heard_by(
    portal_hotel: PortalHotel, ids: dict[str, uuid.UUID], *, turnovers: bool = False
) -> dict[str, set[str | None]]:
    """Кого что зовёт: комнаты невзятых заявок (или выездов) у каждой из службы."""
    heard: dict[str, set[str | None]] = {}
    with tenant_context(portal_hotel.tenant_id):
        tenant_floors = await floors.load_tenant_floors(portal_hotel.tenant_id)
        for key, user_id in ids.items():
            role = StaffRole.SENIOR if key == "saule" else StaffRole.STAFF
            audience = StaffAudience(
                tenant_id=portal_hotel.tenant_id,
                role_key=role,
                service_keys=frozenset(HOUSEKEEPING),
            )
            view = await signal.build_signal(audience, tenant_floors.routing_for(audience, user_id))
            items = view.turnovers if turnovers else view.unclaimed
            heard[key] = {item.room_number for item in items}
    return heard


def _shift_clock(monkeypatch: pytest.MonkeyPatch, delta: timedelta) -> None:
    later = utc_now() + delta
    monkeypatch.setattr(signal, "utc_now", lambda: later)
    monkeypatch.setattr(queue, "utc_now", lambda: later)


async def test_floor_request_calls_floor_team_first_then_whole_service(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """704 — Айгуль, не Дине и не старшей; 1505 (этаж без команды) и «Люкс» —
    всем; через срок `d` 704 слышит вся служба."""
    ids = await _floor_hotel(portal_hotel)
    for room in ("704", "1505", "Люкс"):
        await _make_request(portal_hotel.tenant_id, f"полотенца {room}", room_number=room)

    first = await _heard_by(portal_hotel, ids)
    _shift_clock(monkeypatch, D)
    overdue = await _heard_by(portal_hotel, ids)

    everyone = {"1505", "Люкс"}
    assert first == {
        "aigul": {"704", *everyone},
        "dina": everyone,
        "gulnar": everyone,
        "saule": everyone,
    }
    assert all(rooms == {"704", *everyone} for rooms in overdue.values())


async def test_urgent_floor_request_calls_everyone_at_once(portal_hotel: PortalHotel) -> None:
    ids = await _floor_hotel(portal_hotel)
    await _make_request(portal_hotel.tenant_id, "потоп", room_number="704", is_urgent=True)

    heard = await _heard_by(portal_hotel, ids)

    assert all(rooms == {"704"} for rooms in heard.values())


async def test_deactivated_maid_leaves_her_floor_to_the_service(
    portal_hotel: PortalHotel,
) -> None:
    """Строка раздачи отключённой горничной не действует: 7-й этаж без команды."""
    ids = await _floor_hotel(portal_hotel)
    await staff_team.deactivate_member(
        ids["aigul"], portal_hotel.tenant_id, actor_user_id=portal_hotel.user_id
    )
    await _make_request(portal_hotel.tenant_id, "полотенца", room_number="704")
    del ids["aigul"]

    heard = await _heard_by(portal_hotel, ids)

    assert all(rooms == {"704"} for rooms in heard.values())


async def test_switched_off_floors_call_whole_service(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _floor_hotel(portal_hotel)
    await _make_request(portal_hotel.tenant_id, "полотенца", room_number="704")
    _switch_floors_off(monkeypatch)

    heard = await _heard_by(portal_hotel, ids)

    assert all(rooms == {"704"} for rooms in heard.values())


async def test_floors_do_not_narrow_without_cleaning_deadline(portal_hotel: PortalHotel) -> None:
    """Срок уборки выключен — ступеней по времени нет, и этаж сужал бы навсегда:
    заявку и выезд по этажу сразу слышит вся служба (spec 0053 §4, #606)."""
    ids = await _floor_hotel(portal_hotel)
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=None)
    await _make_request(portal_hotel.tenant_id, "полотенца", room_number="704")
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.report_checkout("705")

    requests = await _heard_by(portal_hotel, ids)
    checkouts = await _heard_by(portal_hotel, ids, turnovers=True)

    assert all(rooms == {"704"} for rooms in requests.values())
    assert all(rooms == {"705"} for rooms in checkouts.values())


async def test_tab_signal_uses_distribution_of_logged_in_maid(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Опрос вкладки передаёт в сигнал раздачу вошедшей (api_router)."""
    await _floor_hotel(portal_hotel)
    _, dina_login = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Дина 2")
    with tenant_context(portal_hotel.tenant_id):
        assignments = await housekeeping_api.list_floor_assignments()
    await _make_request(portal_hotel.tenant_id, "полотенца", room_number="704")
    await submit_login(client, dina_login)

    unclaimed = (await client.get(f"/staff/{HOTEL_SLUG}/api/requests/signal")).json()["unclaimed"]

    assert {row.floor for row in assignments} == {7, 8}
    assert unclaimed == []


async def test_floor_checkout_calls_floor_team_then_everyone(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«704 — выезд · Проверьте номер» — Айгуль, через `d` уборки — всей службе."""
    ids = await _floor_hotel(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.report_checkout("704")
        await housekeeping_api.report_checkout("Люкс")

    first = await _heard_by(portal_hotel, ids, turnovers=True)
    _shift_clock(monkeypatch, D)
    later = await _heard_by(portal_hotel, ids, turnovers=True)

    assert first["aigul"] == {"704", "Люкс"}
    assert first["dina"] == first["gulnar"] == {"Люкс"}
    assert all("704" in rooms for rooms in later.values())

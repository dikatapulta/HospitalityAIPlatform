"""Доска «Выезды и уборка» в кабинете (spec 0046 §4–§6, §11, issue #476).

Права каждого шага по уровню и службе (ERR-HOUSEKEEPING-003), меню и 403 доски;
отметка выезда с подтверждением выселения (004) и без хаускипинга в кабинете
(005); «Выселить» на карточке отправляет номер на доску; автовыезд пути не
создаёт; доска рисует группы, замечание и «Сегодня убрано»; забытый путь
прошлого проживания не глотает новый выезд; без `ENABLE_TURNOVER_BOARD` раздела
нет совсем.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
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

TurnoverStatus = housekeeping_api.TurnoverStatus
RECEPTION = (RECEPTION_SERVICE_KEY,)
HOUSEKEEPING = (HOUSEKEEPING_SERVICE_KEY,)


@pytest.fixture(autouse=True)
def _board_enabled() -> Iterator[None]:
    """Доска включена во всех тестах файла (spec 0046 §5). Свой MonkeyPatch, а не
    общий `monkeypatch` — довод `_fake_rate_limit` в conftest."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


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


async def test_home_tile_counts_the_board(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Плитка главной (#606): номеров в пути и непустые шаги — те же, что у доски;
    «Готовы сегодня» есть и при нуле. Технику доска закрыта — плитки нет."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _hotel_with_housekeeping(portal_hotel)
    await _seed_path(portal_hotel, "704")
    inspected = await _seed_path(portal_hotel, "705")
    ready = await _seed_path(portal_hotel, "706")
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.change_turnover_status(inspected, TurnoverStatus.INSPECTED)
        for status in (
            TurnoverStatus.INSPECTED,
            TurnoverStatus.CLEANING,
            TurnoverStatus.CLEANED,
            TurnoverStatus.READY,
        ):
            await housekeeping_api.change_turnover_status(ready, status)
    await _login_as(client, portal_hotel, StaffRole.STAFF, HOUSEKEEPING)

    page = (await client.get(HOME)).text

    assert 'data-tile="turnovers"' in page
    assert 'data-metric="turnovers">2<' in page
    assert "Ждут проверки: <b>1</b>" in page
    assert "Проверены: <b>1</b>" in page
    assert "Готовы сегодня: <b>1</b>" in page
    assert "Убираются:" not in page  # пустой шаг не показывается

    await _login_as(second_client, portal_hotel, StaffRole.STAFF, ("maintenance",))
    assert 'data-tile="turnovers"' not in (await second_client.get(HOME)).text


# ---------------------------------------------------------------------------
# Забытый путь прошлого проживания (Б-1 ревью #481) и выключатель (Б-2)
# ---------------------------------------------------------------------------


async def _forgotten_cleaned_path(portal_hotel: PortalHotel, room: str) -> uuid.UUID:
    """Вчера номер убрали, а «Принять» никто не нажал."""
    stale_id = await _seed_path(portal_hotel, room)
    with tenant_context(portal_hotel.tenant_id):
        for status in (
            housekeeping_api.TurnoverStatus.INSPECTED,
            housekeeping_api.TurnoverStatus.CLEANING,
            housekeeping_api.TurnoverStatus.CLEANED,
        ):
            await housekeeping_api.change_turnover_status(stale_id, status)
        async with session_scope() as session:
            await session.execute(
                text("UPDATE room_turnovers SET reported_at = :at WHERE id = :id"),
                {"at": utc_now() - timedelta(days=1), "id": stale_id},
            )
    return stale_id


@pytest.mark.parametrize("entry", ["stay_card", "report_form"])
async def test_next_checkout_is_not_swallowed_by_a_forgotten_path(
    client: AsyncClient, portal_hotel: PortalHotel, entry: str
) -> None:
    """Следующий гость 705 выезжает: номер снова в «Ждут проверки», а не во
    вчерашнем «Ждут приёмки» — и с карточки, и формой с подтверждением."""
    await _hotel_with_housekeeping(portal_hotel)
    stale_id = await _forgotten_cleaned_path(portal_hotel, "705")
    with tenant_context(portal_hotel.tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="705", check_out_at=utc_now() + timedelta(days=1))
        )
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    if entry == "stay_card":
        response = await client.post(
            f"/staff/{HOTEL_SLUG}/api/stays/{checked.stay.id}/checkout",
            json={},
            headers=SAME_ORIGIN,
        )
        assert response.status_code == 200
        assert response.json()["turnover_url"] == BOARD
    else:
        response = await _report(client, "705", check_out_stay=True)
        assert response.status_code == 200
        assert response.json()["created"] is True

    [fresh] = await _open_paths(portal_hotel)
    assert fresh.id != stale_id
    assert fresh.status is housekeeping_api.TurnoverStatus.REPORTED
    with tenant_context(portal_hotel.tenant_id):
        stale = await housekeeping_api.get_turnover(stale_id)
    assert stale.status is housekeeping_api.TurnoverStatus.CANCELLED
    board = (await client.get(BOARD)).text
    assert "Ждут проверки — 1" in board
    assert "Ждут приёмки" not in board


async def test_switch_off_hides_the_board_and_creates_no_paths(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без `ENABLE_TURNOVER_BOARD` служба «Уборка» у сотрудников раздел не
    включает: меню, страница и действия молчат, «Выселить» только выселяет
    (Б-2 ревью #481: на копии — только после согласования с отелем)."""
    monkeypatch.setenv("ENABLE_TURNOVER_BOARD", "false")
    get_settings.cache_clear()
    await _hotel_with_housekeeping(portal_hotel)
    seeded_id = await _seed_path(portal_hotel, "704")
    with tenant_context(portal_hotel.tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="705", check_out_at=utc_now() + timedelta(days=1))
        )
    await submit_login(client, portal_hotel.login)

    home = await client.get(HOME)
    board = await client.get(BOARD)
    fragment = await client.get(FRAGMENT)
    report = await _report(client, "706")
    step = await _step(client, seeded_id, "inspect")
    checkout = await client.post(
        f"/staff/{HOTEL_SLUG}/api/stays/{checked.stay.id}/checkout", json={}, headers=SAME_ORIGIN
    )

    assert home.status_code == 200
    assert "Выезды и уборка" not in home.text
    assert (board.status_code, fragment.status_code) == (404, 404)
    assert (report.status_code, step.status_code) == (404, 404)
    assert checkout.status_code == 200
    assert checkout.json()["turnover_url"] is None
    with tenant_context(portal_hotel.tenant_id):
        assert await guests_api.find_active_stay("705") is None
    open_paths = await _open_paths(portal_hotel)
    assert [(path.room_number, path.status) for path in open_paths] == [
        ("704", housekeeping_api.TurnoverStatus.REPORTED)
    ]

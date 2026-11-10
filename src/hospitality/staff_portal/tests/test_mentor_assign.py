"""Назначенный экзамен в кабинете (spec 0072 §3): «Назначить» и «Снять» менеджером, строка
над вкладками и «Назначены вам» у сотрудника, ход назначенного экзамена до итога.

Модель — Fake-адаптеры (`ai/mentor/tests/conftest.py::providers`): ни одного платного
вызова. Помощники и фикстуры — из `test_mentor.py` и `test_mentor_exam.py`.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import mentor_assignments
from hospitality.staff_portal.tests.conftest import PortalHotel, submit_login
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    API,
    PAGE,
    Portal,
    _hotel,
    _login,
    _mentor_models,
    _post,
    portal,
)
from hospitality.staff_portal.tests.test_mentor_exam import QUESTION, _exam_hotel, _stream
from tests.test_staff_auth import create_staff_user, unique_login

ASSIGN = "/exam/assignments"


async def _categories(tenant_id: uuid.UUID) -> None:
    with tenant_context(tenant_id):
        for key, name in (("housekeeping", "Уборка"), ("reception", "Ресепшен")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )


async def _employee(portal: Portal, hotel: PortalHotel, *services: str) -> uuid.UUID:
    portal.client.cookies.clear()
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login,
        tenant_id=hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Сотрудник",
        service_keys=services,
    )
    await submit_login(portal.client, staff_login)
    return user_id


async def _as_manager(portal: Portal, hotel: PortalHotel) -> None:
    portal.client.cookies.clear()
    await submit_login(portal.client, hotel.login)


async def test_manager_assigns_and_archives(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _categories(portal_hotel.tenant_id)
    await _as_manager(portal, portal_hotel)

    whole = await _post(portal.client, ASSIGN, {"chapters": None, "service_keys": None})
    assert whole.status_code == 200
    narrow = await _post(portal.client, ASSIGN, {"chapters": [2], "service_keys": ["housekeeping"]})
    assert narrow.status_code == 200

    page = (await portal.client.get(f"{PAGE}/results")).text
    assert "<b>Весь кодекс</b>" in page and "<b>Глава 2. Администратор</b>" in page
    assert '<details class="card mentor-assign">' in page  # назначения есть — форма свёрнута

    unknown = await _post(portal.client, ASSIGN, {"chapters": None, "service_keys": ["spa"]})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (422, "ERR-AUTH-015")
    gone = await _post(portal.client, ASSIGN, {"chapters": [99], "service_keys": None})
    assert (gone.status_code, gone.json()["error"]["code"]) == (409, "ERR-MENTOR-006")
    empty: list[dict[str, object]] = [
        {"chapters": [], "service_keys": None},
        {"chapters": None, "service_keys": []},
    ]
    for body in empty:
        assert (await _post(portal.client, ASSIGN, body)).status_code == 422, body

    archive = f"{ASSIGN}/{narrow.json()['id']}/archive"
    assert (await _post(portal.client, archive, {})).json() == {"archived": True}
    assert (await _post(portal.client, archive, {})).json() == {"archived": False}
    page = (await portal.client.get(f"{PAGE}/results")).text
    assert "<b>Глава 2. Администратор</b>" not in page and "<b>Весь кодекс</b>" in page


async def test_assign_is_manager_only_and_csrf_shielded(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    body: dict[str, object] = {"chapters": None, "service_keys": None}
    for role in (StaffRole.STAFF, StaffRole.SENIOR):
        portal.client.cookies.clear()
        await _login(portal.client, portal_hotel, role)
        assert (await _post(portal.client, ASSIGN, body)).status_code == 403
        archive = f"{ASSIGN}/{uuid.uuid4()}/archive"
        assert (await _post(portal.client, archive, {})).status_code == 403
    await _as_manager(portal, portal_hotel)
    no_origin = await portal.client.post(f"{API}{ASSIGN}", json=body)
    assert no_origin.status_code == 403
    await _hotel(portal_hotel.tenant_id, enabled=False, codex=False)
    assert (await _post(portal.client, ASSIGN, body)).status_code == 404


async def test_employee_sees_own_assignment_and_passes_it(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _exam_hotel(portal_hotel.tenant_id, questions=1, pass_percent=80)
    await _categories(portal_hotel.tenant_id)
    await _as_manager(portal, portal_hotel)
    rooms = await _post(portal.client, ASSIGN, {"chapters": [2], "service_keys": ["housekeeping"]})
    rooms_id = rooms.json()["id"]

    # Ресепшен назначения уборке не видит и начать его не может.
    await _employee(portal, portal_hotel, "reception")
    page = (await portal.client.get(PAGE)).text
    assert "Вам назначен экзамен" not in page
    refused = await _post(portal.client, "/exam/start", {"assignment_id": rooms_id})
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "ERR-MENTOR-006")

    await _employee(portal, portal_hotel, "housekeeping")
    for tab in ("", "/codex", "/exam"):  # строка над вкладками — на всех страницах раздела
        page = (await portal.client.get(f"{PAGE}{tab}")).text
        assert "Вам назначен экзамен: Глава 2. Администратор" in page, tab
    assert mentor_assignments.ASSIGNED_TITLE in page and mentor_assignments.NOT_TAKEN in page
    assert f'data-assignment="{rooms_id}"' in page
    assert mentor_assignments.OWN_HINT in page
    both = {"assignment_id": rooms_id, "chapter_number": 2}
    assert (await _post(portal.client, "/exam/start", both)).status_code == 422

    started = await _post(portal.client, "/exam/start", {"assignment_id": rooms_id})
    assert started.status_code == 200
    attempt_id = started.json()["attempt_id"]
    await _stream(portal, "/exam/next", {"attempt_id": attempt_id}, QUESTION)
    body = {"message": "Поздороваюсь.", "attempt_id": attempt_id, "turn": 1}
    kind, result = await _stream(portal, "/exam/answer", body, "Оценка: верно\nВсё так.")
    assert (kind, result["exam"]["passed"], result["exam"]["percent"]) == ("done", True, 100)

    page = (await portal.client.get(f"{PAGE}/exam")).text
    assert "Вам назначен экзамен" not in page  # сдан — строки над вкладками нет
    assert "Сдан " in page and " — 100 %" in page
    assert f'data-assignment="{rooms_id}"' not in page  # у сданного кнопки нет
    assert "· Глава 2. Администратор</small>" in page  # «Мои попытки» — тема назначения

    # Снятое назначение пропадает у сотрудника и не начинается.
    await _as_manager(portal, portal_hotel)
    await _post(portal.client, f"{ASSIGN}/{rooms_id}/archive", {})
    await _employee(portal, portal_hotel, "housekeeping")
    assert mentor_assignments.ASSIGNED_TITLE not in (await portal.client.get(f"{PAGE}/exam")).text
    archived = await _post(portal.client, "/exam/start", {"assignment_id": rooms_id})
    assert archived.status_code == 409


async def test_two_open_assignments_are_counted_in_the_banner(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _as_manager(portal, portal_hotel)
    await _post(portal.client, ASSIGN, {"chapters": [1, 2], "service_keys": None})
    await _post(portal.client, ASSIGN, {"chapters": None, "service_keys": None})
    await _employee(portal, portal_hotel)
    page = (await portal.client.get(f"{PAGE}/exam")).text
    assert "Вам назначено 2 экзамена" in page
    assert "<b>Главы 1, 2</b><small>О кодексе · Администратор</small>" in page

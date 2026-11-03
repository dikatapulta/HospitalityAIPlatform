"""Страница «Тестовые данные» (issue #740): кнопка демо-картины на staging.

Главное место ревью — граница копии отеля: на копии (отель контура ≠
`demo-hotel` или адрес ≠ `staging.…`), вне staging и в чужом отеле пункта нет,
страница и POST отвечают 404 и ничего не пишут; сотрудник ниже менеджера — 403.
Нажатие строит ту же картину, что `demo_showcase --reset`, без приглашений;
два нажатия разом — одна картина. Текст итога — числа из результата.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import find_hotel
from hospitality.platform.staff_invites import list_pending_invites
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import demo_data_api
from hospitality.staff_portal.tests.conftest import submit_login
from hospitality.tools.demo_showcase import ShowcaseReport, build_showcase
from hospitality.tools.demo_showcase_board import DEMO_REQUESTS, DEMO_STAYS
from hospitality.tools.demo_showcase_staff import DemoShowcaseError
from hospitality.tools.seed import seed_demo_data
from tests.test_demo_showcase_tool import STAGING_ENV
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = "/staff/demo-hotel/demo-data"
ACTION = "/staff/demo-hotel/api/demo-data"
MENU_LINK = f'href="{PAGE}"'
SAME_ORIGIN = {"origin": "https://test"}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
async def demo_hotel(canonical_database: None, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Демо-отель сида и окружение staging — как у команды (`test_demo_showcase_tool`)."""
    _set_env(monkeypatch, STAGING_ENV)
    await seed_demo_data()
    hotel = await find_hotel("demo-hotel")
    assert hotel is not None
    return hotel.tenant_id


async def _login(
    client: AsyncClient, tenant_id: uuid.UUID, role: StaffRole, *, tenant_slug: str = "demo-hotel"
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=tenant_id,
        role=role,
        display_name="Менеджер Демо",
        service_keys=() if role is StaffRole.MANAGER else ("housekeeping",),
    )
    response = await submit_login(client, staff_login, tenant_slug=tenant_slug)
    assert response.status_code == 303


async def _active_stays(tenant_id: uuid.UUID) -> int:
    with tenant_context(tenant_id):
        return len(await guests_api.list_active_stays())


async def test_manager_fills_the_picture_without_invites(
    demo_hotel: uuid.UUID, client: AsyncClient
) -> None:
    await _login(client, demo_hotel, StaffRole.MANAGER)

    home = await client.get("/staff/demo-hotel")
    page = await client.get(PAGE)
    response = await client.post(ACTION, json={}, headers=SAME_ORIGIN)

    assert MENU_LINK in home.text
    assert page.status_code == 200
    assert "Заполнить тестовыми данными" in page.text
    assert "Заменить тестовые данные на свежие?" in page.text
    assert response.status_code == 200, response.text
    assert response.json() == {
        "message": (
            "Готово: 13 гостей, 7 заявок. Одна заявка станет просроченной через 10 минут, "
            "другая — через 3 часа."
        ),
        "stays": len(DEMO_STAYS),
        "requests": len(DEMO_REQUESTS),
    }
    assert await _active_stays(demo_hotel) == len(DEMO_STAYS)
    # Ссылки-приглашения — за командой: кнопка их не выпускает.
    assert not await list_pending_invites(demo_hotel)


@pytest.mark.parametrize(
    "override",
    [
        # Гостевая копия отеля: SENTRY_ENVIRONMENT тоже staging (#389), отель контура — свой…
        {"TELEGRAM_TENANT_SLUG": "pilot-hotel"},
        # …или адрес не staging.
        {"PUBLIC_BASE_URL": "https://app.example.test"},
        {"SENTRY_ENVIRONMENT": "production"},
    ],
)
async def test_outside_staging_there_is_no_item_and_post_refuses(
    demo_hotel: uuid.UUID,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, str],
) -> None:
    await _login(client, demo_hotel, StaffRole.MANAGER)
    _set_env(monkeypatch, override)

    home = await client.get("/staff/demo-hotel")
    page = await client.get(PAGE)
    response = await client.post(ACTION, json={}, headers=SAME_ORIGIN)

    assert MENU_LINK not in home.text
    assert page.status_code == 404
    assert response.status_code == 404
    assert await _active_stays(demo_hotel) == 0


async def test_another_hotel_on_staging_refuses(demo_hotel: uuid.UUID, client: AsyncClient) -> None:
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other Hotel")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _login(client, other_id, StaffRole.MANAGER, tenant_slug="other-hotel")

    home = await client.get("/staff/other-hotel")
    page = await client.get("/staff/other-hotel/demo-data")
    response = await client.post("/staff/other-hotel/api/demo-data", json={}, headers=SAME_ORIGIN)

    assert "/demo-data" not in home.text
    assert page.status_code == 404
    assert response.status_code == 404
    assert await _active_stays(demo_hotel) == 0


@pytest.mark.parametrize("role", [StaffRole.STAFF, StaffRole.SENIOR])
async def test_below_manager_refused(
    demo_hotel: uuid.UUID, client: AsyncClient, role: StaffRole
) -> None:
    await _login(client, demo_hotel, role)

    home = await client.get("/staff/demo-hotel")
    page = await client.get(PAGE)
    response = await client.post(ACTION, json={}, headers=SAME_ORIGIN)

    assert MENU_LINK not in home.text
    assert page.status_code == 403
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-003"
    assert await _active_stays(demo_hotel) == 0


async def test_cross_origin_post_refused(demo_hotel: uuid.UUID, client: AsyncClient) -> None:
    await _login(client, demo_hotel, StaffRole.MANAGER)

    response = await client.post(ACTION, json={}, headers={"origin": "https://evil.test"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-009"
    assert await _active_stays(demo_hotel) == 0


async def test_picture_refusal_is_err_platform_010(
    demo_hotel: uuid.UUID, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _login(client, demo_hotel, StaffRole.MANAGER)

    async def refuse(**_: object) -> ShowcaseReport:
        raise DemoShowcaseError("У демо-отеля нет служб fnb")

    monkeypatch.setattr(demo_data_api, "build_showcase", refuse)
    response = await client.post(ACTION, json={}, headers=SAME_ORIGIN)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR-PLATFORM-010"


async def test_two_presses_at_once_build_one_picture(demo_hotel: uuid.UUID) -> None:
    await create_staff_user("BOSS", tenant_id=demo_hotel, role=StaffRole.MANAGER)
    await asyncio.gather(
        build_showcase(confirmed=True, reset=True, invite=False),
        build_showcase(confirmed=True, reset=True, invite=False),
    )

    assert await _active_stays(demo_hotel) == len(DEMO_STAYS)
    with tenant_context(demo_hotel):
        today = await requests_api.list_requests_created_since(
            created_after=utc_now() - timedelta(hours=1), limit=100
        )
    alive = Counter(
        (request.room_number, request.summary)
        for request in today
        if request.cancel_reason is not requests_api.CancelReason.DUPLICATE
    )
    assert alive == Counter((spec.room, spec.summary) for spec in DEMO_REQUESTS)


def _report(*minutes: int) -> ShowcaseReport:
    return ShowcaseReport(
        lines=[],
        stays=13,
        requests=7,
        overdue_after=[timedelta(minutes=value) for value in minutes],
    )


def test_done_message_words() -> None:
    assert demo_data_api.done_message(_report()) == "Готово: 13 гостей, 7 заявок."
    assert demo_data_api.done_message(_report(1)) == (
        "Готово: 13 гостей, 7 заявок. Одна заявка станет просроченной через 1 минуту."
    )
    assert demo_data_api.done_message(_report(90, 120)) == (
        "Готово: 13 гостей, 7 заявок. Одна заявка станет просроченной через 1 час 30 минут, "
        "другая — через 2 часа."
    )
    one = ShowcaseReport(lines=[], stays=1, requests=2, overdue_after=[])
    assert demo_data_api.done_message(one) == "Готово: 1 гость, 2 заявки."

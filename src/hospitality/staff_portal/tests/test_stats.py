"""Страница «Статистика» (spec 0065 §6 пп. 3–7; issue #704).

Числа считает `requests.staff_stats` — их сценарии в
`modules/requests/tests/test_staff_stats.py`. Здесь — то, что добавляет
страница: права и выключатель, граница старшего, имена и порядок строк,
период по поясу отеля, прочерк при выключенных напоминаниях.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffActionLog, StaffRole, User, UserStatus
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    StaffActor,
    record_staff_action,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import stats
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/stats"
HOME = f"/staff/{HOTEL_SLUG}"
ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture(autouse=True)
def _stats_enabled() -> Iterator[None]:
    """Раздел включён во всех тестах файла — канон `test_floors`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_STAFF_STATS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _category(tenant_id: uuid.UUID, key: str, name: str) -> uuid.UUID:
    with tenant_context(tenant_id):
        created = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name=name)
        )
    return created.id


async def _staff(
    tenant_id: uuid.UUID,
    name: str,
    *,
    role: StaffRole = StaffRole.STAFF,
    service_keys: tuple[str, ...] = (),
) -> StaffActor:
    login = unique_login()
    user_id = await create_staff_user(
        login, tenant_id=tenant_id, role=role, display_name=name, service_keys=service_keys
    )
    return StaffActor(user_id=user_id, display_name=name, login=login)


async def _claim(
    tenant_id: uuid.UUID, actor: StaffActor, category_id: uuid.UUID, *, waited: timedelta
) -> None:
    """Заявка, созданная час назад и взятая через `waited`, — заявкой и строкой журнала."""
    created_at = utc_now() - timedelta(hours=1)
    with tenant_context(tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="guest request",
                room_number="305",
            )
        )
        async with session_scope() as session:
            record_staff_action(
                session,
                actor,
                StaffActionRecord(
                    action=StaffAction.REQUEST_CLAIMED,
                    request_id=created.id,
                    category_id=category_id,
                ),
            )
            (row,) = list(session.new)
            assert isinstance(row, StaffActionLog)
            row.occurred_at = created_at + waited
            # ORM заявки — приватная часть модуля (контракт import-linter), поэтому
            # SQL, как у `test_turnovers`; RLS ограничивает UPDATE своей строкой.
            await session.execute(
                text("UPDATE service_requests SET created_at = :at WHERE id = :id"),
                {"at": created_at, "id": created.id},
            )


def _names(html: str) -> list[str]:
    return re.findall(r"<tr><td>([^<]+)", html)


async def test_page_is_for_senior_and_manager_and_hidden_when_switched_off(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6 пп. 5–6: менеджер видит страницу, пункт меню и быстрое действие;
    сотрудник и ресепшен уровня «Сотрудник» — 403 и без ссылки. Выключатель
    снят — 404 и пункта нет."""
    await submit_login(client, portal_hotel.login)
    page = await client.get(PAGE)
    assert page.status_code == 200
    assert "<h1>Статистика</h1>" in page.text
    assert "Эту страницу видят только старший службы и менеджер." in page.text
    assert "За этот период заявок не было." in page.text
    assert f'href="{PAGE}"' in (await client.get(HOME)).text

    for service_keys in (RECEPTION, ("housekeeping",)):
        second_client.cookies.clear()
        member = await _staff(portal_hotel.tenant_id, "Сотрудник", service_keys=service_keys)
        assert member.login is not None
        await submit_login(second_client, member.login)
        assert (await second_client.get(PAGE)).status_code == 403
        assert f'href="{PAGE}"' not in (await second_client.get(HOME)).text

    monkeypatch.setenv("ENABLE_STAFF_STATS", "false")
    get_settings.cache_clear()
    assert (await client.get(PAGE)).status_code == 404
    assert f'href="{PAGE}"' not in (await client.get(HOME)).text


async def test_senior_sees_own_service_claims_only(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 5: старшая уборки видит ресепшениста с его заявкой уборки, но не
    инженера; менеджер — всех."""
    await store_hotel_config(portal_hotel.tenant_id)
    housekeeping = await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    engineering = await _category(portal_hotel.tenant_id, "engineering", "Инженерия")
    clerk = await _staff(portal_hotel.tenant_id, "Ресепшен Роза", service_keys=RECEPTION)
    engineer = await _staff(portal_hotel.tenant_id, "Инженер Ерлан", service_keys=("engineering",))
    head = await _staff(
        portal_hotel.tenant_id,
        "Старшая Сауле",
        role=StaffRole.SENIOR,
        service_keys=("housekeeping",),
    )
    await _claim(portal_hotel.tenant_id, clerk, housekeeping, waited=timedelta(minutes=3))
    await _claim(portal_hotel.tenant_id, engineer, engineering, waited=timedelta(minutes=4))

    assert head.login is not None
    await submit_login(second_client, head.login)
    own = (await second_client.get(PAGE)).text
    assert "Только ваши службы: Уборка." in own
    assert _names(own) == ["Ресепшен Роза"]
    await submit_login(client, portal_hotel.login)
    assert _names((await client.get(PAGE)).text) == ["Инженер Ерлан", "Ресепшен Роза"]


async def test_rows_sorted_by_name_erased_account_shows_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 7: строки — по имени, не по числам; учётку стёр суперадмин — вместо
    имени логин из журнала. Под именем — логин из «Сотрудников»."""
    await store_hotel_config(portal_hotel.tenant_id)
    category = await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    busy = await _staff(portal_hotel.tenant_id, "Борис")
    quiet = await _staff(portal_hotel.tenant_id, "айгуль")
    erased = await _staff(portal_hotel.tenant_id, "Стёртый Сотрудник")
    for _ in range(3):
        await _claim(portal_hotel.tenant_id, busy, category, waited=timedelta(minutes=2))
    await _claim(portal_hotel.tenant_id, quiet, category, waited=timedelta(minutes=2))
    await _claim(portal_hotel.tenant_id, erased, category, waited=timedelta(minutes=2))
    async with platform_session_scope() as session:
        user = await session.get(User, erased.user_id)
        assert user is not None
        user.status = UserStatus.ERASED

    await submit_login(client, portal_hotel.login)
    html = (await client.get(PAGE)).text
    # Сортировка — по показанному имени: латинский логин стёртой учётки — раньше кириллицы.
    assert _names(html) == [str(erased.login), "айгуль", "Борис"]
    assert "Стёртый Сотрудник" not in html
    assert f'<span class="stats-login">{busy.login}</span>' in html
    assert "<td>Борис" in html and "<td>3</td>" in html
    assert "взята позже срока службы" in html


async def test_reminders_off_show_dash_and_say_so(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 3: у отеля выключены напоминания — в «Просрочено» прочерк, а под
    таблицей — «напоминания выключены» вместо срока службы."""
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=None)
    category = await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    maid = await _staff(portal_hotel.tenant_id, "Горничная")
    await _claim(portal_hotel.tenant_id, maid, category, waited=timedelta(minutes=50))

    await submit_login(client, portal_hotel.login)
    html = (await client.get(PAGE)).text
    assert "<td>50 мин</td><td>—</td><td>—</td><td>0</td>" in html
    assert "<b>Просрочено</b> — напоминания выключены." in html


def test_period_starts_at_hotel_midnight() -> None:
    """§6 п. 4: 09:00 по Алматы 16.10 — «Сегодня» с полуночи отеля, то есть
    23:30 15.10 по отелю в него не входит; «7 дней» — с полуночи 10.10."""
    now = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    since, until = stats.period_bounds("today", now=now, zone=ALMATY)
    assert (since, until) == (datetime(2026, 10, 15, 19, 0, tzinfo=UTC), now)
    assert datetime(2026, 10, 15, 23, 30, tzinfo=ALMATY) < since
    week, _ = stats.period_bounds("7d", now=now, zone=ALMATY)
    assert week == datetime(2026, 10, 10, 0, 0, tzinfo=ALMATY)
    assert datetime(2026, 10, 9, 23, 59, tzinfo=ALMATY) < week
    month, _ = stats.period_bounds("30d", now=now, zone=ALMATY)
    assert month == datetime(2026, 9, 17, 0, 0, tzinfo=ALMATY)


def test_unknown_period_falls_back_to_week() -> None:
    assert stats.parse_stats_period("nope") == "7d"
    assert stats.parse_stats_period(None) == "7d"
    assert stats.parse_stats_period("30d") == "30d"
    assert stats.stats_path(HOTEL_SLUG, period="7d") == PAGE
    assert stats.stats_path(HOTEL_SLUG, period="today") == f"{PAGE}?period=today"

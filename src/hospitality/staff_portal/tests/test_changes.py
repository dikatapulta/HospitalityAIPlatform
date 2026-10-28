"""Журнал изменений кабинета (spec 0041 §6 пп. 1 и 6; issue #370).

Действия идут через HTTP-маршруты кабинета, а не через сервисы напрямую: на
основном пути `StaffContext` приходит из звена резолвера, и логин обязан быть
заполнен именно там (ревью PR #445). Страница: роль, карточка на главной,
фильтры (граница суток — по поясу отеля), пагинация.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole
from hospitality.platform.staff_actions import (
    StaffActionRead,
    StaffActionRecord,
    StaffActor,
    record_staff_action,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import changes
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
API = f"/staff/{HOTEL_SLUG}/api"
CHANGES_PAGE = f"/staff/{HOTEL_SLUG}/changes"


async def _category(tenant_id: uuid.UUID) -> requests_api.RequestCategoryRead:
    with tenant_context(tenant_id):
        return await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )


async def _guest_request(tenant_id: uuid.UUID, category_id: uuid.UUID) -> uuid.UUID:
    """Заявка от гостя — без личности, строки журнала не даёт."""
    with tenant_context(tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="guest request",
                room_number="220",
            )
        )
    return created.id


async def _journal(tenant_id: uuid.UUID) -> list[StaffActionLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(
                await session.scalars(select(StaffActionLog).order_by(StaffActionLog.occurred_at))
            )


async def _post(client: AsyncClient, path: str, payload: dict[str, object] | None = None) -> int:
    response = await client.post(f"{API}{path}", json=payload or {}, headers=SAME_ORIGIN)
    return response.status_code


async def test_every_cabinet_action_writes_one_row_with_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 1: девять действий — девять строк, у каждой логин отеля и верные
    снапшоты; число ночей продления — то, что передал кабинет."""
    await store_hotel_config(portal_hotel.tenant_id)
    category = await _category(portal_hotel.tenant_id)
    claimed_id = await _guest_request(portal_hotel.tenant_id, category.id)
    cancelled_id = await _guest_request(portal_hotel.tenant_id, category.id)
    await submit_login(client, portal_hotel.login)

    manual = await client.post(
        f"{API}/requests",
        json={"room_number": "305", "category_id": str(category.id), "summary": "towels"},
        headers=SAME_ORIGIN,
    )
    assert manual.status_code == 200
    assert await _post(client, f"/requests/{claimed_id}/claim") == 200
    assert await _post(client, f"/requests/{claimed_id}/complete", {"note": "partly"}) == 200
    assert await _post(client, f"/requests/{cancelled_id}/cancel", {"note": "dup"}) == 200
    checkin = await client.post(
        f"/staff/{HOTEL_SLUG}/checkin", data={"room": "101", "nights": "1", "guests": "1"}
    )
    assert checkin.status_code == 200
    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.find_active_stay("101")
    assert stay is not None
    assert await _post(client, f"/stays/{stay.id}/move", {"room_number": "102"}) == 200
    assert await _post(client, f"/stays/{stay.id}/extend", {"nights": 2}) == 200
    assert await _post(client, f"/stays/{stay.id}/reissue-code") == 200
    assert await _post(client, f"/stays/{stay.id}/checkout") == 200

    rows = await _journal(portal_hotel.tenant_id)
    assert [row.action for row in rows] == [
        StaffAction.REQUEST_CREATED,
        StaffAction.REQUEST_CLAIMED,
        StaffAction.REQUEST_DONE,
        StaffAction.REQUEST_CANCELLED,
        StaffAction.STAY_CHECKED_IN,
        StaffAction.STAY_MOVED,
        StaffAction.STAY_EXTENDED,
        StaffAction.STAY_CODE_REISSUED,
        StaffAction.STAY_CHECKED_OUT,
    ]
    assert {row.actor_login for row in rows} == {portal_hotel.login}
    assert {row.actor_user_id for row in rows} == {portal_hotel.user_id}
    created, claimed, done, cancelled, checked_in, moved, extended, _, checked_out = rows
    assert created.request_daily_number == manual.json()["daily_number"] == 3
    assert (created.category_name, created.room_number) == ("Уборка", "305")
    assert created.request_service_day is not None
    assert (claimed.request_id, claimed.with_note) == (claimed_id, False)
    assert (done.request_id, done.with_note) == (claimed_id, True)
    assert (cancelled.request_id, cancelled.room_number) == (cancelled_id, "220")
    assert (checked_in.stay_id, checked_in.room_number) == (stay.id, "101")
    assert (moved.room_number, moved.room_number_to) == ("101", "102")
    assert (extended.room_number, extended.nights) == ("102", 2)
    assert checked_out.room_number == "102"


async def test_rejected_action_leaves_no_row(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 1: недопустимый переход (ERR-REQUESTS-003) и занятая комната
    (ERR-GUESTS-002) откатывают действие — и строку журнала вместе с ним."""
    category = await _category(portal_hotel.tenant_id)
    request_id = await _guest_request(portal_hotel.tenant_id, category.id)
    await submit_login(client, portal_hotel.login)
    for room in ("101", "102"):
        response = await client.post(
            f"/staff/{HOTEL_SLUG}/checkin", data={"room": room, "nights": "1", "guests": "1"}
        )
        assert response.status_code == 200
    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.find_active_stay("101")
    assert stay is not None
    assert await _post(client, f"/requests/{request_id}/claim") == 200
    before = len(await _journal(portal_hotel.tenant_id))

    assert await _post(client, f"/requests/{request_id}/claim") == 409
    assert await _post(client, f"/stays/{stay.id}/move", {"room_number": "102"}) == 409

    assert len(await _journal(portal_hotel.tenant_id)) == before == 3


async def test_changes_page_is_for_manager_and_senior_only(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 6: менеджер видит страницу и карточку на главной; ресепшен и
    сотрудник службы уровня «Сотрудник» — 403 и без карточки. Старший видит
    строки своих служб — `test_senior_scope.py` (spec 0045 §3)."""
    await submit_login(client, portal_hotel.login)
    page = await client.get(CHANGES_PAGE)
    assert page.status_code == 200
    assert "Журнал изменений" in page.text
    retention_days = get_settings().messages_retention_days
    assert f"Записи хранятся {retention_days} дней." in page.text
    assert "За этот день записей нет." in page.text
    home = await client.get(f"/staff/{HOTEL_SLUG}")
    assert f'href="/staff/{HOTEL_SLUG}/changes"' in home.text

    for role, service_keys in (
        (StaffRole.STAFF, RECEPTION),
        (StaffRole.STAFF, ()),
        (StaffRole.STAFF, ("housekeeping",)),
    ):
        second_client.cookies.clear()
        staff_login = unique_login()
        await create_staff_user(
            staff_login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=service_keys
        )
        await submit_login(second_client, staff_login)
        assert (await second_client.get(CHANGES_PAGE)).status_code == 403
        home = await second_client.get(f"/staff/{HOTEL_SLUG}")
        assert "Журнал изменений" not in home.text


async def _seed(
    tenant_id: uuid.UUID, actor: StaffActor, record: StaffActionRecord, at: datetime
) -> None:
    """Строка журнала боевым путём, но с заданным моментом: объект ещё не
    сброшен в БД, и default `occurred_at` не сработает поверх явного."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            record_staff_action(session, actor, record)
            (row,) = list(session.new)
            assert isinstance(row, StaffActionLog)
            row.occurred_at = at


async def test_changes_page_filters_by_hotel_day_login_and_action(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 6: день — сутки отеля (Алматы, UTC+5), а не UTC; фильтр
    «Выполнена» берёт и строки с оговоркой; фильтр сотрудника — по логину."""
    await store_hotel_config(portal_hotel.tenant_id)
    manager = StaffActor(user_id=portal_hotel.user_id, display_name="M", login=portal_hotel.login)
    other_login = unique_login()
    other = StaffActor(
        user_id=await create_staff_user(
            other_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
        ),
        display_name="O",
        login=other_login,
    )
    done = StaffActionRecord(
        action=StaffAction.REQUEST_DONE,
        request_id=uuid.uuid4(),
        request_daily_number=12,
        request_service_day=date(2026, 10, 15),
        category_name="Уборка",
        room_number="305",
    )
    # 19:30 UTC 15.10 = 00:30 16.10 по Алматы; 18:30 UTC = 23:30 15.10.
    late = datetime(2026, 10, 15, 19, 30, tzinfo=UTC)
    await _seed(
        portal_hotel.tenant_id,
        manager,
        done.model_copy(update={"with_note": True}),
        late,
    )
    await _seed(
        portal_hotel.tenant_id,
        other,
        done,
        late + timedelta(minutes=1),
    )
    await _seed(
        portal_hotel.tenant_id,
        other,
        StaffActionRecord(action=StaffAction.STAY_EXTENDED, room_number="412", nights=2),
        late + timedelta(minutes=2),
    )
    await _seed(
        portal_hotel.tenant_id,
        manager,
        StaffActionRecord(action=StaffAction.STAY_CHECKED_IN, room_number="999"),
        datetime(2026, 10, 15, 18, 30, tzinfo=UTC),
    )
    await submit_login(client, portal_hotel.login)

    day = (await client.get(CHANGES_PAGE, params={"day": "2026-10-16"})).text
    assert "00:30" in day and "00:32" in day
    assert "999" not in day
    assert "#12 (15.10) · Уборка · 305" in day
    assert "<td>Выполнена с оговоркой</td>" in day
    assert "412 · на 2 ночи" in day
    previous = (await client.get(CHANGES_PAGE, params={"day": "2026-10-15"})).text
    assert "23:30" in previous and "<td>Заселение</td>" in previous
    assert "Уборка" not in previous

    done_only = (
        await client.get(CHANGES_PAGE, params={"day": "2026-10-16", "action": "request_done"})
    ).text
    assert done_only.count("<tr><td>") == 2
    assert "412" not in done_only
    by_other = (
        await client.get(CHANGES_PAGE, params={"day": "2026-10-16", "login": other_login})
    ).text
    assert by_other.count("<tr><td>") == 2
    assert "с оговоркой" not in by_other


async def test_changes_page_paginates_by_200(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§2.4: 200 строк на страницу, «Показать более ранние» ведёт к остатку."""
    await store_hotel_config(portal_hotel.tenant_id)
    actor = StaffActor(user_id=portal_hotel.user_id, display_name="M", login=portal_hotel.login)
    with tenant_context(portal_hotel.tenant_id):
        async with session_scope() as session:
            for index in range(changes.PAGE_SIZE + 1):
                record_staff_action(
                    session,
                    actor,
                    StaffActionRecord(action=StaffAction.STAY_CHECKED_IN, room_number=str(index)),
                )
    await submit_login(client, portal_hotel.login)

    first = (await client.get(CHANGES_PAGE)).text
    assert first.count("<tr><td>") == changes.PAGE_SIZE
    assert "Показать более ранние" in first
    rest = (await client.get(CHANGES_PAGE, params={"offset": str(changes.PAGE_SIZE)})).text
    assert rest.count("<tr><td>") == 1
    assert "Показать более ранние" not in rest


def test_filters_fall_back_to_defaults_instead_of_422() -> None:
    today = date(2026, 10, 16)
    parsed = changes.parse_filters(
        {"day": "9999-12-31", "action": "nope", "offset": "²", "login": ""}, today=today
    )
    assert parsed == changes.ChangesFilters(day=today, login=None, action=None, offset=0)
    assert changes.parse_filters({"offset": "99999999999999999999"}, today=today).offset == (
        1_000_000
    )


def test_nights_word_agrees_with_number() -> None:
    words = {count: changes.nights_word(count) for count in (1, 2, 5, 11, 12, 21, 22, 25)}
    assert words == {
        1: "ночь",
        2: "ночи",
        5: "ночей",
        11: "ночей",
        12: "ночей",
        21: "ночь",
        22: "ночи",
        25: "ночей",
    }


def test_auto_checkout_row_is_signed_automatic_not_no_login() -> None:
    """Автовыезд (spec 0043 §2.3) пишется без личности: «кто» — «автоматически»,
    а не «без логина» учётки, которой нет."""
    row = StaffActionRead(
        action=StaffAction.STAY_AUTO_CHECKED_OUT,
        stay_id=uuid.uuid4(),
        room_number="305",
        occurred_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        actor_login=None,
    )
    rendered = changes._row(row, UTC, date(2026, 10, 21))
    assert rendered == {
        "time": "09:00",
        "login": "автоматически",
        "action": "Выезд по сроку",
        "subject": "305",
    }

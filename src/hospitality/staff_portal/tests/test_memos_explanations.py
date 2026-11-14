"""«Служебные записки» — экраны объяснительных (spec 0077 §6, §9, §12 PR 5б; #936).

Требование по HTTP: менеджер прикладывает фото — сотруднику пуш без имён, строка «Ждут
меня» и число в меню; фото — только этим двоим и без кэша; «Получил(а)» паролем через
окно подтверждения — срок на странице обеим сторонам. Объяснительная по требованию:
адресат — затребовавший, «Подать» — пуш ему, на карточке «Подана» и нет «Решить», в его
«Ждут меня» её нет; после срока — отметка; «Дополнить» — новая запись со ссылкой. Без
требования — примечание адресату. Права: `staff` не требует (страница — 404, загрузка —
403), `senior` — только у своих служб, и форма предлагает только их. Поиск находит
требование по имени, а условие на статус его не берёт.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.modules.memos import api as memos_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.staff_portal import memos_search
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.tests.conftest import PortalHotel
from hospitality.staff_portal.tests.test_memos_screens import (
    API,
    MEMOS,
    SAME_ORIGIN,
    RecordingPush,
    _confirmed,
    _hotel,
    _login,
    _member,
    _phone,
    _pushes,
    app,  # noqa: F401  (фикстура для pytest)
    fake_push,  # noqa: F401  (фикстура для pytest)
    shared_redis,  # noqa: F401  (фикстура для pytest)
    stand,  # noqa: F401  (фикстура для pytest)
)
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

PHOTO = make_png((800, 1100))
BOSS = "Ерлан Менеджер"
EMPLOYEE = "Айгуль Сотрудник"
TITLE = "Опоздание 10.11"


async def _demand(client: AsyncClient, employee_id: uuid.UUID) -> str:
    created = await client.post(
        f"{API}/demands",
        data={"employee_id": str(employee_id)},
        files={"photo": ("demand.png", PHOTO, "image/png")},
        headers=SAME_ORIGIN,
    )
    assert created.status_code == 200, created.text
    demand_id: str = created.json()["id"]
    assert created.json()["href"].endswith(f"/memos/demands/{demand_id}")
    return demand_id


async def _received(client: AsyncClient, demand_id: str) -> None:
    """«Получил(а)» окном подтверждения паролем кабинета: begin → complete."""
    intent = {"kind": "receive"}
    begun = await client.post(
        f"{API}/demands/{demand_id}/begin", json={"intent": intent}, headers=SAME_ORIGIN
    )
    assert begun.status_code == 200, begun.text
    done = await client.post(
        f"{API}/demands/{demand_id}/complete",
        json={"intent": intent, "challenge_id": begun.json()["challenge_id"], "password": PASSWORD},
        headers=SAME_ORIGIN,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "received"


async def _explanation(client: AsyncClient, basis: dict[str, str]) -> str:
    created = await client.post(
        f"{API}/explanations",
        json={"title": TITLE, "body": "Опоздал из-за пробки на Абая.", **basis},
        headers=SAME_ORIGIN,
    )
    assert created.status_code == 200, created.text
    memo_id: str = created.json()["id"]
    return memo_id


async def test_demand_and_explanation_over_http(
    stand: dict[str, AsyncClient],
    portal_hotel: PortalHotel,
    fake_push: RecordingPush,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _hotel(portal_hotel.tenant_id)
    employee, boss, stranger = stand["author"], stand["boss"], stand["stranger"]
    employee_id = await _member(employee, portal_hotel, EMPLOYEE)
    boss_id = await _member(boss, portal_hotel, BOSS, StaffRole.MANAGER)
    await _member(stranger, portal_hotel, "Посторонний")
    employee_phone = await _phone(portal_hotel.tenant_id, employee_id)
    boss_phone = await _phone(portal_hotel.tenant_id, boss_id)

    form = await boss.get(f"{MEMOS}/demands/new")
    assert form.status_code == 200 and EMPLOYEE in form.text and texts.DEMAND_INTRO in form.text
    demand_id = await _demand(boss, employee_id)
    [push] = _pushes(fake_push, employee_phone)
    assert push["body"] == texts.PUSH_DEMAND and push["url"].endswith(f"/demands/{demand_id}")
    assert "Ерлан" not in json.dumps(push, ensure_ascii=False)

    waiting = await employee.get(MEMOS)
    assert texts.DEMAND_TITLE in waiting.text and "Не получено" in waiting.text
    assert 'aria-label="ждут вас: 1"' in waiting.text
    page = f"{MEMOS}/demands/{demand_id}"
    assert texts.DEMAND_NOT_RECEIVED in (await boss.get(page)).text
    assert texts.DEMAND_TO_RECEIVE.format(boss=BOSS) in (await employee.get(page)).text
    photo = await employee.get(f"{page}/photo")
    assert photo.status_code == 200 and photo.headers["content-type"].startswith("image/")
    assert photo.headers["cache-control"] == "no-store"
    assert (await stranger.get(page)).status_code == 404
    assert (await stranger.get(f"{page}/photo")).status_code == 404
    # Писать объяснение — только после «Получил(а)» (§6.2).
    by_demand = f"{MEMOS}/explanations/new?demand={demand_id}"
    assert (await employee.get(by_demand)).status_code == 404

    await _received(employee, demand_id)
    seen = (await employee.get(page)).text
    assert "Срок — до конца дня" in seen and "Получение: подтверждено паролем кабинета" in seen
    assert "Сотрудник подтвердил получение." in (await boss.get(page)).text
    assert 'aria-label="ждут вас' not in (await employee.get(MEMOS)).text

    form = await employee.get(by_demand)
    assert form.status_code == 200 and texts.EXPLANATION_TO.format(name=BOSS) in form.text
    memo_id = await _explanation(employee, {"demand_id": demand_id})
    assert await _confirmed(employee, memo_id, {"kind": "send"}) == "submitted"
    [push] = _pushes(fake_push, boss_phone)
    assert push["body"] == texts.PUSH_EXPLANATION and "Опоздание" not in json.dumps(push)

    card = (await boss.get(f"{MEMOS}/{memo_id}")).text
    for expected in ("Подана", "Подал(а)", "По требованию от"):
        assert expected in card
    assert 'value="approved"' not in card and texts.NO_DEMAND_NOTE not in card
    assert TITLE not in (await boss.get(MEMOS)).text  # решения не ждёт (§6)
    explained = (await boss.get(page)).text
    assert texts.DEMAND_EXPLAINED in explained and "ОБ-" in explained
    assert "Объяснение подано" in (await boss.get(f"{MEMOS}/mine")).text

    # Подана после срока — отметка на карточке (срок сдвинут в прошлое двойником).
    real_get_demand = memos_api.get_demand

    async def overdue(staff: StaffContext, demand: uuid.UUID) -> memos_api.DemandCard:
        found = await real_get_demand(staff, demand)
        return found.model_copy(update={"due_at": datetime(2026, 1, 5, 18, 59, tzinfo=UTC)})

    with monkeypatch.context() as patched:
        patched.setattr(memos_api, "get_demand", overdue)
        late = (await boss.get(f"{MEMOS}/{memo_id}")).text
    assert texts.LATE.format(day="05.01.2026") in late

    assert texts.SUPPLEMENT in (await employee.get(f"{MEMOS}/{memo_id}")).text
    assert (await boss.get(f"{MEMOS}/{memo_id}/supplement")).status_code == 404
    supplement_form = await employee.get(f"{MEMOS}/{memo_id}/supplement")
    assert supplement_form.status_code == 200 and "Дополнение к ОБ-" in supplement_form.text
    added = await employee.post(
        f"{API}/{memo_id}/supplement",
        json={"title": "Дополнение", "body": "Прилагаю справку о ДТП."},
        headers=SAME_ORIGIN,
    )
    assert added.status_code == 200, added.text
    supplement_id = added.json()["id"]
    assert "Дополнение к" in (await employee.get(f"{MEMOS}/{supplement_id}")).text
    edit = await employee.get(f"{MEMOS}/{supplement_id}/edit")
    assert edit.status_code == 200 and "data-explanation-draft" in edit.text


async def test_explanation_without_demand_notes_the_addressee(
    stand: dict[str, AsyncClient], portal_hotel: PortalHotel, fake_push: RecordingPush
) -> None:
    await _hotel(portal_hotel.tenant_id)
    author, boss = stand["author"], stand["boss"]
    await _member(author, portal_hotel, EMPLOYEE)
    boss_id = await _member(boss, portal_hotel, "Ерлан Старший", StaffRole.SENIOR)

    form = await author.get(f"{MEMOS}/explanations/new")
    assert form.status_code == 200 and "Ерлан Старший" in form.text
    memo_id = await _explanation(author, {"addressee_id": str(boss_id)})
    edit = await author.get(f"{MEMOS}/{memo_id}/edit")
    assert edit.status_code == 200 and texts.EXPLANATION_DRAFT in edit.text
    assert "Подать" in (await author.get(f"{MEMOS}/{memo_id}")).text
    assert await _confirmed(author, memo_id, {"kind": "send"}) == "submitted"
    assert texts.NO_DEMAND_NOTE in (await boss.get(f"{MEMOS}/{memo_id}")).text
    assert texts.NO_DEMAND_NOTE not in (await author.get(f"{MEMOS}/{memo_id}")).text


async def _with_services(
    client: AsyncClient, hotel: PortalHotel, name: str, role: StaffRole, services: tuple[str, ...]
) -> uuid.UUID:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login,
        tenant_id=hotel.tenant_id,
        role=role,
        display_name=name,
        service_keys=services,
    )
    await _login(client, staff_login, PASSWORD)
    return user_id


async def test_only_managers_and_own_service_seniors_demand(
    stand: dict[str, AsyncClient], portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff, senior = stand["author"], stand["boss"]
    staff_id = await _with_services(
        staff, portal_hotel, "Дана Горничная", StaffRole.STAFF, ("housekeeping",)
    )
    await _with_services(senior, portal_hotel, "Ерлан Старший", StaffRole.SENIOR, ("housekeeping",))
    cook_id = await _with_services(
        stand["signer"], portal_hotel, "Бек Повар", StaffRole.STAFF, ("fnb",)
    )

    assert (await staff.get(f"{MEMOS}/demands/new")).status_code == 404
    assert texts.DEMAND_NEW not in (await staff.get(f"{MEMOS}/mine")).text
    refused = await staff.post(
        f"{API}/demands",
        data={"employee_id": str(cook_id)},
        files={"photo": ("demand.png", PHOTO, "image/png")},
        headers=SAME_ORIGIN,
    )
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "ERR-AUTH-003"

    assert texts.DEMAND_NEW in (await senior.get(f"{MEMOS}/mine")).text
    form = (await senior.get(f"{MEMOS}/demands/new")).text
    assert "Дана Горничная" in form and "Бек Повар" not in form and texts.DEMAND_SENIOR_HINT in form
    foreign = await senior.post(
        f"{API}/demands",
        data={"employee_id": str(cook_id)},
        files={"photo": ("demand.png", PHOTO, "image/png")},
        headers=SAME_ORIGIN,
    )
    assert foreign.status_code == 422
    assert foreign.json()["error"]["details"][0]["reason"] == "not_your_service"
    await _demand(senior, staff_id)


def test_search_finds_demand_by_name_not_by_status() -> None:
    zone = ZoneInfo("Asia/Almaty")
    boss, employee = uuid.uuid4(), uuid.uuid4()
    names = {boss: BOSS, employee: EMPLOYEE}
    demand = memos_api.DemandSummary(
        id=uuid.uuid4(),
        demanded_by_id=boss,
        employee_id=employee,
        created_at=datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
        received_at=None,
        due_at=None,
        state=memos_api.DemandState.AWAITING_RECEIPT,
    )

    def found(condition: memos_search.MemoFilter) -> bool:
        return memos_search.matches_demand(demand, condition, names.__getitem__, zone)

    where = memos_search.MemoFilter
    assert found(where()) and found(where(who="айгуль")) and found(where(who="Ерлан"))
    assert found(where(kind=memos_api.MemoKind.EXPLANATION))
    assert found(where(since=date(2026, 11, 10)))
    assert not found(where(who="Дана")) and not found(where(kind=memos_api.MemoKind.MEMO))
    assert not found(where(status=memos_api.MemoStatus.SUBMITTED))
    assert not found(where(text="ОБ-")) and not found(where(until=date(2026, 11, 9)))

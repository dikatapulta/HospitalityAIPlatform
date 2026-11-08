"""«Итоги экзаменов» «Наставника» (spec 0048 §12, issue #780): сводка по службам менеджеру.

Попытки пишутся прямо модулем (`start_exam` → `set_exam_question` → `record_exam_answer`):
ход по API и модель — забота `test_mentor_exam.py`. Помощники и фикстуры — из
`test_mentor.py` (канон этого раздела).
"""

# ruff: noqa: F811

from __future__ import annotations

import re
import uuid

from sqlalchemy import update

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    TenantMembership,
    User,
    UserStatus,
)
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import mentor_results
from hospitality.staff_portal.tests.conftest import PortalHotel, submit_login
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    PAGE,
    Portal,
    _hotel,
    _login,
    _mentor_models,
    portal,
)
from tests.test_staff_auth import create_staff_user, unique_login


async def _member(tenant_id: uuid.UUID, name: str, *services: str) -> uuid.UUID:
    return await create_staff_user(
        unique_login(),
        tenant_id=tenant_id,
        role=StaffRole.STAFF,
        display_name=name,
        service_keys=services,
    )


async def _exam(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    grades: list[mentor_api.QuizGrade],
    *,
    chapter: int | None = None,
) -> None:
    """Законченная попытка из `len(grades)` вопросов с порогом 70 %."""
    with tenant_context(tenant_id):
        attempt = await mentor_api.start_exam(
            user_id, chapter_number=chapter, question_count=len(grades), pass_percent=70
        )
        for grade in grades:
            asked = await mentor_api.set_exam_question(
                user_id,
                attempt.id,
                turn=attempt.turn,
                article_number="2",
                question="Что вы сделаете?",
            )
            attempt = await mentor_api.record_exam_answer(
                user_id, attempt.id, turn=asked.turn, article_number="2", grade=grade
            )
        assert attempt.finished


def _card(page: str, title: str) -> str:
    """Текст карточки службы `title` без тегов — от её <details> до конца."""
    match = re.search(
        rf"<details[^>]*>\s*<summary><b>{re.escape(title)}</b>.*?</details>", page, re.S
    )
    assert match is not None, title
    return re.sub(r"<[^>]+>", "", match.group(0))


CORRECT, PARTIAL, WRONG = (
    mentor_api.QuizGrade.CORRECT,
    mentor_api.QuizGrade.PARTIAL,
    mentor_api.QuizGrade.WRONG,
)


async def test_results_grouped_by_service_with_last_attempt(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    tenant_id = portal_hotel.tenant_id
    await _hotel(tenant_id)
    with tenant_context(tenant_id):
        for key, name in (("housekeeping", "Уборка"), ("reception", "Ресепшен")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
    aigul = await _member(tenant_id, "Айгуль", "reception", "housekeeping")
    erlan = await _member(tenant_id, "Ерлан", "reception")
    await _member(tenant_id, "Дана", "housekeeping")
    gone = await _member(tenant_id, "Ушедший Сотрудник", "reception")
    erased = await _member(tenant_id, "Стёртый Сотрудник", "reception")
    # У Айгуль итог — вторая попытка (100 %), а не первая проваленная.
    await _exam(tenant_id, aigul, [WRONG, WRONG])
    await _exam(tenant_id, aigul, [CORRECT, CORRECT], chapter=2)
    await _exam(tenant_id, erlan, [CORRECT, PARTIAL, WRONG])  # 50 % — не сдан
    await _exam(tenant_id, gone, [CORRECT])
    await _exam(tenant_id, erased, [CORRECT])
    # Начатая и брошенная попытка Ерлана итог не меняет.
    with tenant_context(tenant_id):
        await mentor_api.start_exam(erlan, chapter_number=None, question_count=5, pass_percent=70)
    async with platform_session_scope() as session:
        await session.execute(
            update(TenantMembership)
            .where(TenantMembership.user_id == gone)
            .values(status=MembershipStatus.REVOKED)
        )
        await session.execute(
            update(User).where(User.id == erased).values(status=UserStatus.ERASED)
        )

    await submit_login(portal.client, portal_hotel.login)
    response = await portal.client.get(f"{PAGE}/results")
    assert response.status_code == 200
    page = response.text
    assert "Итоги экзаменов" in page and "Кто работает в двух службах, виден в обеих." in page
    assert "Ушедший Сотрудник" not in page and "Стёртый Сотрудник" not in page

    titles = re.findall(r"<summary><b>(.*?)</b>", page)
    assert titles == ["Ресепшен", "Уборка", "Без службы"]  # ресепшен первым, менеджер — без службы

    reception = _card(page, "Ресепшен")
    assert "2 сотрудника · сдали 1 · не сдали 1 · в среднем 75 %" in reception
    assert reception.index("Айгуль") < reception.index("Ерлан")  # по имени, без рейтинга
    assert "Глава 2. " in reception and "100 %, сдан" in reception
    assert "Весь кодекс" in reception and "50 %, не сдан" in reception

    housekeeping = _card(page, "Уборка")  # Айгуль — и здесь
    assert "2 сотрудника · сдали 1 · не проходили 1 · в среднем 100 %" in housekeeping
    assert "Дана" in housekeeping and mentor_results.NO_ATTEMPTS in housekeeping

    assert "1 сотрудник · не проходили 1" in _card(page, "Без службы")


async def test_results_tab_is_manager_only(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    for role in (StaffRole.STAFF, StaffRole.SENIOR):
        portal.client.cookies.clear()
        await _login(portal.client, portal_hotel, role)
        assert (await portal.client.get(f"{PAGE}/results")).status_code == 403
        assert "Итоги экзаменов" not in (await portal.client.get(PAGE)).text

    portal.client.cookies.clear()
    await submit_login(portal.client, portal_hotel.login)
    page = (await portal.client.get(f"{PAGE}/results")).text
    assert "Пока никто не закончил экзамен" in page
    assert f'href="{PAGE}/results"' in (await portal.client.get(PAGE)).text


def test_summary_parts_and_average() -> None:
    assert mentor_results.average_percent([]) is None
    assert mentor_results.average_percent([70, 69]) == 69  # 69,5 — вниз, как процент экзамена
    parts = mentor_results.summary_parts(members=12, passed=7, failed=3, average=76)
    assert " · ".join(parts) == (
        "12 сотрудников · сдали 7 · не сдали 3 · не проходили 2 · в среднем 76 %"
    )
    assert mentor_results.summary_parts(members=4, passed=0, failed=0, average=None) == (
        ["4 сотрудника", "не проходили 4"]
    )

"""«Проект кодекса» и «Неточность в статье» в кабинете (spec 0072 §6, варианты (б)).

Пометка на «Кодексе» и «Экзамене», кнопка отметки (любой уровень, своя, CSRF), число
отметивших на «Где ошибаются» и «на проекте» в «Итогах экзаменов». Модель — Fake-адаптеры:
ни одного платного вызова. Помощники и фикстуры — из `test_mentor.py` и `test_mentor_results.py`.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import re
import uuid

from hospitality.ai.mentor import codex_upload
from hospitality.ai.mentor.tests.conftest import CODEX, codex_files, providers
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_team
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import mentor_assignments, mentor_flags, mentor_results
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
from hospitality.staff_portal.tests.test_mentor_results import _exam, _member
from tests.test_staff_auth import create_staff_user, unique_login

DRAFT_CODEX = CODEX.replace(
    "Название: Кодекс отеля «Тест»\n", "Название: Кодекс отеля «Тест»\nСтатус: Проект\n"
)
FLAGS = "/flags"


async def _draft(tenant_id: uuid.UUID) -> None:
    """Отель с «Наставником» и кодексом-проектом («Статус: Проект» в шапке)."""
    await _hotel(tenant_id, codex=False)
    with tenant_context(tenant_id):
        await codex_upload.upload_codex(
            codex_files(DRAFT_CODEX), uploaded_by_user_id=None, providers=providers()
        )


def _mine(page: str) -> list[str]:
    state = re.search(r'id="mentor-flags-state">(.+?)</script>', page)
    assert state is not None
    mine: list[str] = json.loads(state.group(1))["mine"]
    return mine


async def test_draft_marker_on_codex_and_exam(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _draft(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    codex = (await portal.client.get(f"{PAGE}/codex")).text
    assert mentor_flags.DRAFT_BADGE in codex and mentor_flags.DRAFT_CODEX_TEXT in codex
    assert codex.count('class="btn btn-quiet mentor-flag" data-article=') == 3  # у каждой статьи
    exam = (await portal.client.get(f"{PAGE}/exam")).text
    assert mentor_flags.DRAFT_EXAM_TEXT in exam and 'id="mentor-flags-state"' in exam

    await _hotel(portal_hotel.tenant_id)  # утверждённый кодекс — шапка без «Статус: проект»
    codex = (await portal.client.get(f"{PAGE}/codex")).text
    assert mentor_flags.DRAFT_BADGE not in codex and mentor_flags.DRAFT_CODEX_TEXT not in codex
    assert mentor_flags.DRAFT_EXAM_TEXT not in (await portal.client.get(f"{PAGE}/exam")).text
    assert 'id="mentor-flags-state"' not in (await portal.client.get(PAGE)).text  # «Спросить»


async def test_flag_is_own_and_any_level(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    flagged = await _post(portal.client, FLAGS, {"article_number": "2", "flagged": True})
    assert flagged.status_code == 200 and flagged.json() == {"flagged": True}
    assert _mine((await portal.client.get(f"{PAGE}/codex")).text) == ["2"]
    assert _mine((await portal.client.get(f"{PAGE}/quiz")).text) == ["2"]
    missing = await _post(portal.client, FLAGS, {"article_number": "99", "flagged": True})
    assert missing.json() == {"flagged": False}

    portal.client.cookies.clear()  # другой сотрудник отметку первого не видит и не снимает
    await _login(portal.client, portal_hotel, StaffRole.MANAGER, name="Менеджер")
    assert _mine((await portal.client.get(f"{PAGE}/codex")).text) == []
    await _post(portal.client, FLAGS, {"article_number": "2", "flagged": False})
    members = await staff_team.list_tenant_members(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        counts = await mentor_api.article_flag_counts(user_ids=[m.user_id for m in members])
    assert [(c.article_number, c.count) for c in counts] == [("2", 1)]


async def test_flag_is_shielded(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    body = {"article_number": "2", "flagged": True}
    assert (await _post(portal.client, FLAGS, body)).status_code in (401, 403)  # не вошёл
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    assert (await portal.client.post(f"{API}{FLAGS}", json=body)).status_code == 403  # CSRF
    for bad in ({"article_number": "2; DROP", "flagged": True}, {"article_number": "2"}):
        assert (await _post(portal.client, FLAGS, bad)).status_code == 422, bad
    await _hotel(portal_hotel.tenant_id, enabled=False, codex=False)
    assert (await _post(portal.client, FLAGS, body)).status_code == 404


async def test_stats_show_counts_without_names(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.SENIOR, name="Старший")
    page = (await portal.client.get(f"{PAGE}/stats")).text
    assert mentor_flags.FLAGS_TITLE in page and mentor_flags.FLAGS_EMPTY in page

    first = await _member(portal_hotel.tenant_id, "Айгерим")
    second = await _member(portal_hotel.tenant_id, "Дана")
    with tenant_context(portal_hotel.tenant_id):
        for user_id in (first, second):
            await mentor_api.set_article_flag(user_id, "2", flagged=True)
        await mentor_api.set_article_flag(first, "3", flagged=True)
    page = (await portal.client.get(f"{PAGE}/stats")).text
    flags = page[page.index(mentor_flags.FLAGS_TITLE) :]
    assert flags.index("Статья 2. Встреча гостя у стойки") < flags.index("Статья 3. Проверка брони")
    assert "2 сотрудника" in flags and "1 сотрудник<" in flags
    assert "Айгерим" not in flags and "Дана" not in flags


async def test_draft_attempt_is_trial(portal: Portal, portal_hotel: PortalHotel) -> None:
    """Вариант (в): сдача на проекте — пробная: у сотрудника экзамен по-прежнему назначен,
    в «Итогах» — «не сдан · пробно»; зачётная сдача после утверждения кодекса её сменяет."""
    tenant_id = portal_hotel.tenant_id
    await _draft(tenant_id)
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=tenant_id, role=StaffRole.STAFF, display_name="Айгерим"
    )
    with tenant_context(tenant_id):
        assignment = await mentor_api.create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
    await _exam(tenant_id, user_id, [mentor_api.QuizGrade.CORRECT], assignment=assignment.id)

    await submit_login(portal.client, staff_login)
    exam = (await portal.client.get(f"{PAGE}/exam")).text
    assert mentor_assignments.TRIAL_STATE.format(percent=100) in exam
    assert "exam-assigned-start" in exam and "Вам назначен экзамен" in exam
    assert f"· {mentor_assignments.TRIAL_MARK}</small>" in exam  # «Мои попытки»

    portal.client.cookies.clear()
    await submit_login(portal.client, portal_hotel.login)
    page = (await portal.client.get(f"{PAGE}/results")).text
    assert f"не сдан · {mentor_results.TRIAL} 100 %" in page and mentor_results.TRIAL_NOTE in page
    assert "<span>не сдали 1</span>" in page and "в среднем" not in page

    await _hotel(tenant_id)  # кодекс утвердили: та же загрузка без «Статус: проект»
    await _exam(tenant_id, user_id, [mentor_api.QuizGrade.CORRECT], assignment=assignment.id)
    page = (await portal.client.get(f"{PAGE}/results")).text
    assert "сдан — 100 %" in page and mentor_results.TRIAL_NOTE not in page

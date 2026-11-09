"""Вкладка «Экзамен» «Наставника» (spec 0048 §12): страница, ход по API, итог, права.

Модель — Fake-адаптеры (`ai/mentor/tests/conftest.py::providers`): ни одного платного
вызова. Помощники и фикстуры — из `test_mentor.py` (канон этого раздела).
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from hospitality.ai.mentor import codex_upload
from hospitality.ai.mentor.tests.conftest import codex_files, providers
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import mentor
from hospitality.staff_portal.tests.conftest import PortalHotel
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    PAGE,
    Portal,
    _events,
    _hotel,
    _login,
    _mentor_models,
    _post,
    portal,
)

QUESTION = "Гость подходит, пока вы говорите по телефону. Что вы сделаете?"


async def _exam_hotel(tenant_id: uuid.UUID, *, questions: int, pass_percent: int) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                mentor_enabled=True,
                mentor_exam_question_count=questions,
                mentor_exam_pass_percent=pass_percent,
            ),
        )
    with tenant_context(tenant_id):
        await codex_upload.upload_codex(
            codex_files(), uploaded_by_user_id=None, providers=providers()
        )


def _state(page: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="mentor-exam-state">(.*?)</script>', page
    )
    assert match is not None
    state: dict[str, Any] = json.loads(match.group(1))
    return state


async def _stream(portal: Portal, path: str, body: dict[str, Any], answer: str) -> Any:
    portal.answer_with(providers(answer=answer))
    response = await _post(portal.client, path, body)
    assert response.status_code == 200
    return _events(response.text)[-1]


async def test_exam_runs_to_result_and_lands_in_my_attempts(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _exam_hotel(portal_hotel.tenant_id, questions=2, pass_percent=80)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)

    page = (await portal.client.get(f"{PAGE}/exam")).text
    assert "2 вопроса подряд — как в тренировке, но с итогом." in page
    assert "Экзамен сдан, если набрано 80 % и больше." in page
    assert "Начать экзамен" in page and "Мои попытки" not in page
    assert _state(page)["progress"] is None

    started = await _post(portal.client, "/exam/start", {"chapter_number": None})
    assert started.status_code == 200
    attempt_id = started.json()["attempt_id"]
    assert (started.json()["total"], started.json()["pass_percent"]) == (2, 80)

    kind, asked = await _stream(portal, "/exam/next", {"attempt_id": attempt_id}, QUESTION)
    assert (kind, asked["answer"], asked["exam"]["turn"]) == ("done", QUESTION, 1)

    # Обновили страницу посреди экзамена — открытый вопрос на месте, с номером, а полоса
    # хода над лентой — с темой и счётом (#888).
    page = (await portal.client.get(f"{PAGE}/exam")).text
    assert QUESTION in page and "Вопрос 1 из 2" in page
    assert '<div id="exam-progress" class="mentor-progress">' in page
    assert "<span>Весь кодекс</span> · " in page and "верно 0 · частично 0 · неверно 0" in page
    assert _state(page)["progress"]["attempt_id"] == attempt_id

    body = {"message": "Закончу звонок.", "attempt_id": attempt_id, "turn": 1}
    kind, checked = await _stream(portal, "/exam/answer", body, "Оценка: верно\nВсё так.")
    assert (kind, checked["exam"]["answered"], checked["exam"]["finished"]) == ("done", 1, False)

    await _stream(portal, "/exam/next", {"attempt_id": attempt_id}, QUESTION)
    body = {"message": "Извинюсь.", "attempt_id": attempt_id, "turn": 2}
    kind, result = await _stream(portal, "/exam/answer", body, "Оценка: частично верно\nМало.")
    assert (result["exam"]["finished"], result["exam"]["percent"]) == (True, 75)
    assert result["exam"]["passed"] is False  # порог отеля — 80 %

    page = (await portal.client.get(f"{PAGE}/exam")).text
    assert "Мои попытки · 1" in page
    assert 'Последний экзамен: <b class="mentor-fail">75 %, не сдан</b>' in page
    assert _state(page)["progress"] is None


async def test_stale_turn_gets_error_event_with_text(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _exam_hotel(portal_hotel.tenant_id, questions=3, pass_percent=70)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    attempt_id = (await _post(portal.client, "/exam/start", {})).json()["attempt_id"]
    await _stream(portal, "/exam/next", {"attempt_id": attempt_id}, QUESTION)

    body = {"message": "Ответ", "attempt_id": attempt_id, "turn": 5}
    kind, error = await _stream(portal, "/exam/answer", body, "Оценка: верно")
    assert (kind, error["message"]) == ("error", mentor.NO_EXAM_QUESTION_TEXT)


async def test_colleague_cannot_answer_my_attempt(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _exam_hotel(portal_hotel.tenant_id, questions=3, pass_percent=70)
    await _login(portal.client, portal_hotel, StaffRole.STAFF, name="Әсел")
    attempt_id = (await _post(portal.client, "/exam/start", {})).json()["attempt_id"]
    await _stream(portal, "/exam/next", {"attempt_id": attempt_id}, QUESTION)

    await _login(portal.client, portal_hotel, StaffRole.MANAGER, name="Менеджер")
    body = {"message": "Ответ", "attempt_id": attempt_id, "turn": 1}
    kind, error = await _stream(portal, "/exam/answer", body, "Оценка: верно")
    assert (kind, error["message"]) == ("error", mentor.NO_EXAM_QUESTION_TEXT)
    assert _state((await portal.client.get(f"{PAGE}/exam")).text)["progress"] is None


async def test_exam_is_closed_with_the_section(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id, enabled=False, codex=False)
    await _login(portal.client, portal_hotel, StaffRole.MANAGER)
    assert (await portal.client.get(f"{PAGE}/exam")).status_code == 404
    assert (await _post(portal.client, "/exam/start", {})).status_code == 404


async def test_bad_bodies_are_rejected_before_the_model(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _exam_hotel(portal_hotel.tenant_id, questions=3, pass_percent=70)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    attempt_id = str(uuid.uuid4())
    for path, body in (
        ("/exam/start", {"chapter_number": 0}),
        ("/exam/next", {"attempt_id": "nope"}),
        ("/exam/answer", {"message": " ", "attempt_id": attempt_id, "turn": 1}),
        ("/exam/answer", {"message": "Ответ", "attempt_id": attempt_id, "turn": 0}),
    ):
        assert (await _post(portal.client, path, body)).status_code == 422, path

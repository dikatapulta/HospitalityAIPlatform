"""Сквозной путь запуска 01.12 без модели (issue #1051 п. 3): сотрудник и гость.

Сотрудник — от приглашения менеджера до своей строки в «Итогах экзаменов»: приглашение →
вход → отметка согласия → «Наставник» → назначенный экзамен → итоги. Гость — от кода
заселения до карточки ЧП в кабинете: код → вопрос на трёх языках → слово о ЧП. Всё идёт
по HTTP одного приложения, как идут люди, разными браузерами; модель — Fake-адаптеры
шлюза (ADR-007), платных вызовов нет.

Каждый шаг подробно проверен своим тестом (приглашение — `test_invite_pages.py`, отметка —
`test_ai_consent.py`, экзамен — `test_mentor_assign.py`, итоги — `test_mentor_results.py`,
веб-чат — `channels/web/tests/test_web_chat.py`, ЧП — `test_escalation_requests.py`); здесь —
что шаги стыкуются: служба из приглашения доходит до назначения и итогов, отметка открывает
раздел, ЧП гостя с кодом становится карточкой ресепшена. Те тесты готовят начало шага сами
(сотрудник — `create_staff_user` сразу со всеми отметками, попытка — прямо модулем), поэтому
цепочку целиком не проверяет ни один из них. Чего этот тест не видит — язык и смысл ответа
живой модели и JavaScript страниц: это прогон по эффектам на кандидате (#1051 п. 1) и платная
часть по «да» основателя (п. 4).

Гостевой ход идёт в режиме «только консультации» — единственном у копии отеля
(`ENABLE_SERVICE_REQUESTS` выключен по умолчанию): инструмента заявки в запросе нет.
F811 отключён на файл: параметр-фикстура обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hospitality.ai import urgency
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider
from hospitality.ai.mentor import codex_upload
from hospitality.ai.mentor.tests.conftest import codex_files, providers
from hospitality.app import create_app
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.staff_auth import resolve_staff_session
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.mentor_api import get_mentor_providers
from hospitality.staff_portal.tests.conftest import (  # noqa: F401  (фикстуры для pytest)
    HOTEL_SLUG,
    PortalHotel,
    _fake_rate_limit,
    portal_hotel,
    submit_login,
)
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    _events,
    _mentor_models,
)
from tests.test_staff_auth import PASSWORD

HOME = f"/staff/{HOTEL_SLUG}"
API = f"{HOME}/api"
MENTOR = f"{HOME}/mentor"
SAME_ORIGIN = {"origin": "https://test"}
HOUSEKEEPING = "housekeeping"
RECEPTION_PHONE = "+7 727 000-00-00"
ROOM = "101"
GUEST_CHAT = f"/g/{HOTEL_SLUG}/{ROOM}"

# Вопрос гостя на каждом языке копии и ответ Fake-модели на него: ход обязан дойти до
# модели и вернуть гостю её текст, на каком бы языке гость ни спросил.
GUEST_QUESTIONS = (
    ("Во сколько завтрак?", "Завтрак с 7:00 до 10:30 на втором этаже."),
    ("Таңғы ас қашан?", "Таңғы ас 7:00-ден 10:30-ға дейін, екінші қабатта."),
    ("What time is breakfast?", "Breakfast is served from 7:00 to 10:30 on the second floor."),
)
EMERGENCY = "Пожар в номере, дым!"


@dataclass
class Launch:
    """Одно приложение и три браузера со своими cookie: менеджер, новый сотрудник, гость."""

    tenant_id: uuid.UUID
    app: FastAPI
    manager: AsyncClient
    employee: AsyncClient
    guest: AsyncClient
    guest_model: ScriptedLlmProvider

    def mentor_answers(self, text: str) -> None:
        """Следующий ход «Наставника» — Fake-модель с этим текстом (канон `test_mentor.py`)."""
        self.app.dependency_overrides[get_mentor_providers] = lambda: providers(answer=text)


@pytest.fixture
async def launch(portal_hotel: PortalHotel) -> AsyncIterator[Launch]:
    await _configure_hotel(portal_hotel.tenant_id)
    guest_model = ScriptedLlmProvider([MockTurn(text=answer) for _, answer in GUEST_QUESTIONS])
    app = create_app()
    app.dependency_overrides[get_mentor_providers] = lambda: providers()
    app.dependency_overrides[get_web_llm_provider] = lambda: guest_model
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="https://test") as manager,
        AsyncClient(transport=transport, base_url="https://test") as employee,
        AsyncClient(transport=transport, base_url="https://test") as guest,
    ):
        assert (await submit_login(manager, portal_hotel.login)).status_code == 303
        yield Launch(portal_hotel.tenant_id, app, manager, employee, guest, guest_model)


async def _configure_hotel(tenant_id: uuid.UUID) -> None:
    """Отель, как его настраивают к запуску: «Наставник» с кодексом и экзаменом из одного
    вопроса, службы уборки и ресепшена, ресепшен — служба эскалаций, телефон ресепшена."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=RECEPTION_PHONE,
                escalation_category_key=RECEPTION_SERVICE_KEY,
                mentor_enabled=True,
                mentor_exam_question_count=1,
                mentor_exam_pass_percent=80,
            ),
        )
    with tenant_context(tenant_id):
        for key, name in ((HOUSEKEEPING, "Уборка"), (RECEPTION_SERVICE_KEY, "Ресепшен")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
        await codex_upload.upload_codex(
            codex_files(), uploaded_by_user_id=None, providers=providers()
        )


async def _post(client: AsyncClient, path: str, body: dict[str, Any]) -> Any:
    return await client.post(path, json=body, headers=SAME_ORIGIN)


async def _mentor_turn(launch: Launch, path: str, body: dict[str, Any], answer: str) -> Any:
    """Ход экзамена потоком: последнее событие обязано быть `done`, а не `error`."""
    launch.mentor_answers(answer)
    response = await _post(launch.employee, f"{API}/mentor{path}", body)
    assert response.status_code == 200, response.text
    kind, data = _events(response.text)[-1]
    assert kind == "done", data
    return data


def _results_card(page: str, topic: str, service: str) -> str:
    """Текст карточки службы `service` в разделе назначения `topic` «Итогов экзаменов»."""
    section = re.search(
        rf'<section class="mentor-assignment">\s*<div class="mentor-assignment-head">\s*<div>\s*'
        rf"<b>{re.escape(topic)}</b>.*?</section>",
        page,
        re.S,
    )
    assert section is not None, topic
    card = re.search(
        rf"<details[^>]*>\s*<summary><b>{re.escape(service)}</b>.*?</details>",
        section.group(0),
        re.S,
    )
    assert card is not None, service
    return re.sub(r"<[^>]+>", "", card.group(0))


async def test_employee_goes_from_invite_to_exam_results(launch: Launch) -> None:
    # 1. Менеджер приглашает сотрудника уборки — ссылка одна, её он передаёт сам.
    invited = await _post(
        launch.manager,
        f"{API}/team/invites",
        {
            "invited_name": "Айгерим",
            "login": "aige",
            "role_key": "staff",
            "service_keys": [HOUSEKEEPING],
        },
    )
    assert invited.status_code == 200, invited.text
    invite_path = urlsplit(invited.json()["invite_url"]).path

    # 2. Сотрудник открывает ссылку, придумывает пароль и сразу оказывается в кабинете.
    invite_page = await launch.employee.get(invite_path)
    assert invite_page.status_code == 200
    assert "Ваш логин — <b>AIGE</b>" in invite_page.text
    accepted = await launch.employee.post(
        invite_path, data={"password": PASSWORD}, headers=SAME_ORIGIN
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"{HOME}/requests"
    assert (await launch.employee.get(f"{HOME}/requests")).status_code == 200

    # Следующая смена: вышел — входит логином из приглашения и своим паролем.
    await launch.employee.post("/staff/logout", headers=SAME_ORIGIN)
    assert (await launch.employee.get(f"{HOME}/requests")).status_code == 303
    assert (await submit_login(launch.employee, "aige")).status_code == 303
    session_token = launch.employee.cookies.get("staff_session")
    assert session_token is not None
    session = await resolve_staff_session(session_token)
    assert session is not None
    employee_id = session.user_id

    # 3. Без отметки согласия раздел ИИ закрыт (в бою отметок по умолчанию нет).
    closed = await launch.employee.get(MENTOR)
    assert closed.status_code == 403 and "Раздел закрыт" in closed.text

    # 4. Менеджер ставит отметку в «Сотрудниках» — «Наставник» открыт со следующего запроса.
    marked = await _post(
        launch.manager, f"{API}/team/members/{employee_id}/ai-consent", {"recipients": ["openai"]}
    )
    assert marked.status_code == 200, marked.text
    assert (await launch.employee.get(MENTOR)).status_code == 200
    assert f'href="{MENTOR}"' in (await launch.employee.get(HOME)).text

    # 5. Менеджер назначает экзамен по главе 2 уборке.
    assigned = await _post(
        launch.manager,
        f"{API}/mentor/exam/assignments",
        {"chapters": [2], "service_keys": [HOUSEKEEPING]},
    )
    assert assigned.status_code == 200, assigned.text
    assignment_id = assigned.json()["id"]

    # 6. Сотрудник видит назначение и сдаёт его: вопрос → ответ → оценка.
    exam_page = (await launch.employee.get(f"{MENTOR}/exam")).text
    assert "Вам назначен экзамен: Глава 2. Администратор" in exam_page
    assert f'data-assignment="{assignment_id}"' in exam_page
    started = await _post(
        launch.employee, f"{API}/mentor/exam/start", {"assignment_id": assignment_id}
    )
    assert started.status_code == 200, started.text
    attempt_id = started.json()["attempt_id"]
    question = "Гость подходит, пока вы говорите по телефону. Что вы сделаете?"
    asked = await _mentor_turn(launch, "/exam/next", {"attempt_id": attempt_id}, question)
    assert asked["answer"] == question
    answer = {"message": "Закончу звонок и поздороваюсь.", "attempt_id": attempt_id, "turn": 1}
    graded = await _mentor_turn(launch, "/exam/answer", answer, "Оценка: верно\nВсё так.")
    assert graded["exam"]["finished"] is True
    assert (graded["exam"]["passed"], graded["exam"]["percent"]) == (True, 100)
    assert "Вам назначен экзамен" not in (await launch.employee.get(f"{MENTOR}/exam")).text

    # 7. Менеджер видит сдачу в «Итогах экзаменов» — в разделе назначения, в карточке уборки.
    results = await launch.manager.get(f"{MENTOR}/results")
    assert results.status_code == 200
    card = _results_card(results.text, "Глава 2. Администратор", "Уборка")
    assert "1 сотрудник · сдали 1 · в среднем 100 %" in card
    assert "Айгерим" in card and "сдан — 100 %" in card


async def test_guest_goes_from_code_to_emergency_card(launch: Launch) -> None:
    with tenant_context(launch.tenant_id):
        stay = await check_in(
            StayCheckIn(
                room_number=ROOM,
                check_out_at=utc_now() + timedelta(days=1),
                guest_display_name="Wang Li",
            )
        )

    # 1. Гость сканирует QR: экран входа — он же экран согласия действующей версии.
    entry = await launch.guest.get(GUEST_CHAT)
    assert entry.status_code == 200 and CONSENT_VERSION in entry.text

    # 2. Код заселения привязывает чат к проживанию.
    bound = await _post(launch.guest, f"{GUEST_CHAT}/session", {"code": stay.access_code})
    assert bound.status_code == 200, bound.text

    # 3. Вопрос на каждом из трёх языков доходит до модели и возвращает гостю её ответ.
    for question, model_answer in GUEST_QUESTIONS:
        replied = await _post(launch.guest, f"{GUEST_CHAT}/messages", _message(question))
        assert replied.status_code == 200, replied.text
        assert replied.json()["replies"][0] == model_answer
    assert len(launch.guest_model.calls) == len(GUEST_QUESTIONS)
    offered = {tool.name for call in launch.guest_model.calls for tool in call.tools}
    assert "create_service_request" not in offered  # режим «только консультации»

    # 4. Слово о ЧП: модель не зовётся, гость получает телефон ресепшена.
    alarm = await _post(launch.guest, f"{GUEST_CHAT}/messages", _message(EMERGENCY))
    assert alarm.status_code == 200, alarm.text
    assert alarm.json()["replies"] == [urgency.emergency_reply("ru", RECEPTION_PHONE)]
    assert len(launch.guest_model.calls) == len(GUEST_QUESTIONS)

    # 5. ЧП — срочная карточка ресепшена с комнатой и проживанием гостя …
    with tenant_context(launch.tenant_id):
        categories = await requests_api.list_categories()
        reception = next(c.id for c in categories if c.key == RECEPTION_SERVICE_KEY)
        (card,) = await requests_api.list_open_requests(limit=10, category_ids=[reception])
    assert card.origin is requests_api.ServiceRequestOrigin.ESCALATION
    assert card.is_urgent and card.room_number == ROOM and card.stay_id == stay.stay.id

    # … и менеджер видит её в очереди кабинета — со словами гостя и пометкой «Срочно».
    assert card.summary == f"ЧП — сообщение гостя: «{EMERGENCY}»"
    queue = await launch.manager.get(f"{HOME}/requests")
    assert queue.status_code == 200
    assert card.summary in queue.text
    assert 'class="badge badge-urgent">Срочно<' in queue.text


def _message(text: str) -> dict[str, str]:
    return {"text": text, "client_message_id": str(uuid.uuid4())}

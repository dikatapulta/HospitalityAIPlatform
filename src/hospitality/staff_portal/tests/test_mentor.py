"""«Наставник» в кабинете (spec 0048 §6–7, §11 PR 4; issue #492).

Права по §6: чат и кодекс — любому уровню, «Где ошибаются» — старшему (без имён)
и менеджеру (с именами и «Обнулить»), загрузка кодекса — менеджеру. Флаг отеля
выключен — пункта меню нет, страницы и действия — 404. Поток: `delta`…, затем
`done`; ошибка хода — событие `error` с текстом сотруднику (ERR-AI-002 — лимит
дня). Пустой и слишком длинный текст — 422 до потока (Н-11); стёртая учётка в
статистике не видна (Н-17), а `user_id` в логах ответа «Тренировки» нет.
Провайдеры шлюза — Fake-адаптеры через `dependency_overrides` (ADR-007).
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from structlog.testing import capture_logs

from hospitality.ai.mentor import codex_upload
from hospitality.ai.mentor.common import MAX_MESSAGE_CHARS, MentorProviders
from hospitality.ai.mentor.tests.conftest import CODEX, codex_files, providers
from hospitality.app import create_app
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, User, UserStatus
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import mentor
from hospitality.staff_portal.mentor_api import MentorStreamResponse, get_mentor_providers
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
PAGE = f"{HOME}/mentor"
API = f"{HOME}/api/mentor"
SAME_ORIGIN = {"origin": "https://test"}


@pytest.fixture(autouse=True)
def _mentor_models() -> Iterator[None]:
    """Назначение `mentor` настроено (канон `ai/mentor/tests/conftest.py`); свой
    MonkeyPatch — довод `_fake_rate_limit` в conftest кабинета."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("LLM_MODEL_MENTOR", "gpt-6-luna")
    patcher.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
    # Ключ нужен только проверке старта (`validate_configured_model`): провайдеры
    # подменены Fake-адаптерами, в OpenAI вызов не уходит.
    patcher.setenv("OPENAI_API_KEY", "test-key-never-sent")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@dataclass
class Portal:
    app: FastAPI
    client: AsyncClient

    def answer_with(self, fakes: MentorProviders) -> None:
        self.app.dependency_overrides[get_mentor_providers] = lambda: fakes


@pytest.fixture
async def portal() -> AsyncIterator[Portal]:
    app = create_app()
    app.dependency_overrides[get_mentor_providers] = lambda: providers()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield Portal(app=app, client=client)


async def _hotel(tenant_id: uuid.UUID, *, enabled: bool = True, codex: bool = True) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                mentor_enabled=enabled,
            ),
        )
    if codex:
        with tenant_context(tenant_id):
            await codex_upload.upload_codex(
                codex_files(), uploaded_by_user_id=None, providers=providers()
            )


async def _login(
    client: AsyncClient, hotel: PortalHotel, role: StaffRole, name: str = "Әсел"
) -> uuid.UUID:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=hotel.tenant_id, role=role, display_name=name
    )
    await submit_login(client, staff_login)
    return user_id


async def _post(client: AsyncClient, path: str, body: dict[str, Any]) -> Any:
    return await client.post(f"{API}{path}", json=body, headers=SAME_ORIGIN)


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_disabled_section_is_404_and_out_of_menu(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id, enabled=False, codex=False)
    await submit_login(portal.client, portal_hotel.login)

    assert PAGE not in (await portal.client.get(HOME)).text
    assert (await portal.client.get(PAGE)).status_code == 404
    assert (await portal.client.get(f"{PAGE}/codex")).status_code == 404
    assert (
        await _post(portal.client, "/ask", {"message": "Как встретить гостя?"})
    ).status_code == 404
    assert (await _post(portal.client, "/stats/reset", {})).status_code == 404


async def test_rights_follow_spec_matrix(portal: Portal, portal_hotel: PortalHotel) -> None:
    """§6: сотруднику — чат и кодекс; «Где ошибаются», загрузка и «Обнулить» — нет."""
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)

    home = (await portal.client.get(HOME)).text
    assert f'href="{PAGE}"' in home
    chat = await portal.client.get(PAGE)
    assert chat.status_code == 200 and "Чем помочь на смене?" in chat.text
    assert f"{PAGE}/stats" not in chat.text
    codex = await portal.client.get(f"{PAGE}/codex")
    assert codex.status_code == 200 and "Встреча гостя у стойки" in codex.text
    assert "codex-upload" not in codex.text
    assert (await portal.client.get(f"{PAGE}/stats")).status_code == 403
    assert (await portal.client.get(f"{PAGE}/unknown")).status_code == 404

    upload = await _post(portal.client, "/codex", {"files": [{"name": "a.md", "text": CODEX}]})
    assert upload.status_code == 403
    assert (await _post(portal.client, "/stats/reset", {})).status_code == 403


async def test_ask_streams_deltas_then_done_and_keeps_history(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    portal.answer_with(providers(answer="Поздоровайтесь в течение десяти секунд [Статья 2]."))

    response = await _post(portal.client, "/ask", {"message": "  Как встретить гостя?  "})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "done" and set(kinds[:-1]) == {"delta"} and len(kinds) > 2
    done = events[-1][1]
    assert "".join(data["text"] for kind, data in events[:-1]) == done["answer"]
    assert done["sources"] == [{"number": "2", "title": "Встреча гостя у стойки", "anchor": "st-2"}]

    page = (await portal.client.get(PAGE)).text
    assert "Как встретить гостя?" in page
    assert f'href="{PAGE}/codex#st-2">Статья 2. Встреча гостя у стойки' in page

    # «Начать заново» стирает только свой диалог.
    assert (await _post(portal.client, "/reset", {"mode": "ask"})).json() == {"deleted": True}
    assert "Как встретить гостя?" not in (await portal.client.get(PAGE)).text


async def test_stream_errors_become_error_event_with_staff_text(
    portal: Portal, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hotel(portal_hotel.tenant_id, codex=False)
    staff_id = await _login(portal.client, portal_hotel, StaffRole.STAFF)

    no_codex = _events((await _post(portal.client, "/ask", {"message": "Вопрос"})).text)
    assert no_codex == [("error", {"code": "ERR-MENTOR-002", "message": mentor.NO_CODEX_TEXT})]

    with tenant_context(portal_hotel.tenant_id):
        await codex_upload.upload_codex(
            codex_files(), uploaded_by_user_id=None, providers=providers()
        )
    monkeypatch.setenv("LLM_MENTOR_DAILY_BUDGET_USD", "0")
    get_settings.cache_clear()
    budget = _events((await _post(portal.client, "/ask", {"message": "Вопрос"})).text)
    assert budget == [("error", {"code": "ERR-AI-002", "message": mentor.BUDGET_TEXT})]
    with tenant_context(portal_hotel.tenant_id):  # ход с ошибкой диалог не меняет
        assert await mentor_api.get_conversation(staff_id, mentor_api.ConversationMode.ASK) is None


async def test_blank_long_and_cross_origin_requests_are_rejected_before_stream(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)

    assert (await _post(portal.client, "/ask", {"message": "   "})).status_code == 422
    long = "а" * (MAX_MESSAGE_CHARS + 1)
    assert (await _post(portal.client, "/quiz/answer", {"message": long})).status_code == 422
    foreign = await portal.client.post(f"{API}/ask", json={"message": "Вопрос"})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "ERR-AUTH-009"


async def test_quiz_round_counts_in_stats_without_user_id_in_logs(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_id = await _login(portal.client, portal_hotel, StaffRole.STAFF, name="Айгерим")

    portal.answer_with(providers(answer="Гость у стойки ждёт, а вы на звонке. Что сделаете?"))
    question = _events((await _post(portal.client, "/quiz/new", {"chapter_number": 2})).text)
    assert question[-1][0] == "done" and question[-1][1]["sources"] == []
    assert 'placeholder="Ваш ответ…"' in (await portal.client.get(f"{PAGE}/quiz")).text

    portal.answer_with(providers(answer="Оценка: неверно\nСначала поздоровайтесь."))
    with capture_logs() as logs:
        verdict = _events(
            (await _post(portal.client, "/quiz/answer", {"message": "Закончу звонок"})).text
        )
    assert verdict[-1][0] == "done"
    assert verdict[-1][1]["sources"][0]["number"] in {"2", "3"}
    assert any(entry["event"] == "mentor_quiz_answered" for entry in logs)
    assert not [entry for entry in logs if str(staff_id) in json.dumps(entry, default=str)]

    again = _events((await _post(portal.client, "/quiz/answer", {"message": "Ещё раз"})).text)
    assert again == [("error", {"code": "ERR-MENTOR-003", "message": mentor.NO_QUESTION_TEXT})]


async def test_stats_names_only_for_manager_and_erased_users_hidden(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    aigerim = await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Айгерим",
    )
    erased = await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Стёртый Сотрудник",
    )
    with tenant_context(portal_hotel.tenant_id):
        article = (await mentor_api.get_articles(["2"]))["2"]
        for user_id, grade in (
            (aigerim, mentor_api.QuizGrade.WRONG),
            (erased, mentor_api.QuizGrade.WRONG),
            (erased, mentor_api.QuizGrade.WRONG),
        ):
            await _graded(user_id, article, grade)
    async with platform_session_scope() as session:
        await session.execute(
            update(User).where(User.id == erased).values(status=UserStatus.ERASED)
        )

    await _login(portal.client, portal_hotel, StaffRole.SENIOR)
    senior = await portal.client.get(f"{PAGE}/stats")
    assert senior.status_code == 200
    assert "Глава 2. Администратор" in senior.text and "<td>1</td>" in senior.text
    assert "Айгерим" not in senior.text and "stats-reset" not in senior.text

    portal.client.cookies.clear()
    await submit_login(portal.client, portal_hotel.login)
    manager = (await portal.client.get(f"{PAGE}/stats")).text
    assert "По сотрудникам" in manager and "Айгерим" in manager
    assert "Стёртый Сотрудник" not in manager
    assert (await _post(portal.client, "/stats/reset", {})).json() == {"deleted": 3}
    assert "Пока никто не отвечал" in (await portal.client.get(f"{PAGE}/stats")).text


async def _graded(
    user_id: uuid.UUID, article: mentor_api.CodexArticleRead, grade: mentor_api.QuizGrade
) -> None:
    question = mentor_api.NewMentorMessage(
        role=mentor_api.MessageRole.ASSISTANT, text="Что сделаете?", created_at=utc_now()
    )
    await mentor_api.start_quiz_question(user_id, article_number=article.number, question=question)
    answer = mentor_api.NewMentorMessage(
        role=mentor_api.MessageRole.USER, text="Ответ", created_at=utc_now()
    )
    verdict = mentor_api.NewMentorMessage(
        role=mentor_api.MessageRole.ASSISTANT, text="Оценка: неверно", created_at=utc_now()
    )
    await mentor_api.finish_quiz_answer(
        user_id, article=article, answer=answer, verdict=verdict, grade=grade
    )


async def test_manager_uploads_codex_and_markup_error_keeps_old(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id, codex=False)
    await submit_login(portal.client, portal_hotel.login)
    empty = (await portal.client.get(f"{PAGE}/codex")).text
    assert "codex-upload" in empty and mentor.NO_CODEX_TEXT in empty

    uploaded = await _post(
        portal.client, "/codex", {"files": [{"name": "10_kodeks.md", "text": CODEX}]}
    )
    assert uploaded.json() == {"title": "Кодекс отеля «Тест»", "article_count": 3}

    broken = CODEX.replace("#### Статья 3.", "#### Статья 5.")
    failed = await _post(
        portal.client, "/codex", {"files": [{"name": "10_kodeks.md", "text": broken}]}
    )
    assert failed.status_code == 422
    error = failed.json()["error"]
    assert error["code"] == "ERR-MENTOR-001" and error["message"].startswith(
        "Файл 10_kodeks.md, строка"
    )
    page = (await portal.client.get(f"{PAGE}/codex")).text
    assert "Статья 3." in page and "Сейчас: загружен" in page


async def test_stream_is_closed_when_employee_leaves() -> None:
    """Сотрудник закрыл вкладку посреди ответа: генератор закрывается сразу, а не
    сборщиком мусора, — шлюз успевает записать оборванный вызов в контексте запроса."""
    closed: list[bool] = []

    async def body() -> AsyncIterator[str]:
        try:
            yield "event: delta\ndata: {}\n\n"
            yield "event: delta\ndata: {}\n\n"
        finally:
            closed.append(True)

    sent: list[str] = []

    async def send(message: Any) -> None:
        sent.append(message["type"])
        if message["type"] == "http.response.body":
            raise OSError("client gone")

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    response = MentorStreamResponse(body(), media_type="text/event-stream")
    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed == [True]
    assert sent == ["http.response.start", "http.response.body"]

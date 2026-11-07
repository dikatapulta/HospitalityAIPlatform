"""«Предложить идею» в кабинете (spec 0071 §8–9, §12 PR 3; #871).

Флаг отеля выключен — пункта меню нет, страницы и действия 404. Интервью — 403 для
`staff`; «Все идеи», «Разобрано» и выгрузка — 403 менеджеру без флага суперадмина.
Ход — поток `delta`/`done`, в `done` черновик без пометки ИИ; правка и отправка
черновика модель не зовут; повторная отправка — ERR-MENTOR-005; «Мои идеи» без
пометки. `{читатель}` — суперадмины отеля. Выгрузка — только свой отель. Карта
возможностей называет каждый раздел меню кабинета. Провайдер шлюза — Fake-адаптер.
"""

# ruff: noqa: F811

from __future__ import annotations

import ast
import importlib
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.mentor.common import MentorProviders
from hospitality.ai.mentor.ideas import IDEA_DRAFT_LABEL
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import propose_idea_card
from hospitality.app import create_app
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, Tenant, User
from hospitality.platform.totp import code_at, time_step
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import ideas, ideas_all, shell
from hospitality.staff_portal.mentor_api import get_mentor_providers
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login
from tests.test_staff_superadmin import create_superadmin

HOME = f"/staff/{HOTEL_SLUG}"
PAGE = f"{HOME}/ideas"
API = f"{HOME}/api/ideas"
SAME_ORIGIN = {"origin": "https://test"}
SUMMARY = "Оплата мини-бара по ссылке на выезде, без очереди у стойки."
MARK_NOTE = "NG-3: карты не храним; возможна оплата по ссылке провайдера"


@pytest.fixture(autouse=True)
def _ideas_model() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("LLM_MODEL_IDEAS", "claude-sonnet-5")
    patcher.setenv("ANTHROPIC_API_KEY", "sk-ant-test-never-sent")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@dataclass
class Portal:
    app: FastAPI
    client: AsyncClient

    def answer_with(self, *turns: MockTurn) -> ScriptedLlmProvider:
        chat = ScriptedLlmProvider(list(turns), model="claude-sonnet-5")
        self.app.dependency_overrides[get_mentor_providers] = lambda: MentorProviders(chat=chat)
        return chat


@pytest.fixture
async def portal() -> AsyncIterator[Portal]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield Portal(app=app, client=client)


async def _hotel(tenant_id: uuid.UUID, *, enabled: bool = True) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                ideas_enabled=enabled,
            ),
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


async def _superadmin(client: AsyncClient, hotel: PortalHotel) -> uuid.UUID:
    staff_login = unique_login()
    user_id, secret = await create_superadmin(hotel.tenant_id, staff_login)
    response = await client.post(
        f"{HOME}/superadmin",
        data={
            "login": staff_login.lower(),
            "password": PASSWORD,
            "code": code_at(secret, time_step(utc_now())),
        },
    )
    assert response.status_code == 303
    return user_id


async def _post(client: AsyncClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return await client.post(f"{API}{path}", json=body or {}, headers=SAME_ORIGIN)


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _card_call(summary: str = SUMMARY) -> ToolCall:
    return ToolCall(
        id="call-1",
        name=propose_idea_card.NAME,
        arguments={"summary": summary, "mark": "constraint", "mark_note": MARK_NOTE},
    )


async def _sent_idea(tenant_id: uuid.UUID, user_id: uuid.UUID, summary: str) -> uuid.UUID:
    """Отправленная идея прямо через модуль — как её оставил бы ход и «Отправить»."""
    with tenant_context(tenant_id):
        conversation = await mentor_api.append_messages(
            user_id,
            mentor_api.ConversationMode.IDEA,
            [
                mentor_api.NewMentorMessage(
                    role=mentor_api.MessageRole.USER,
                    text=f"Сказал: {summary}",
                    created_at=utc_now(),
                )
            ],
        )
        draft = await mentor_api.save_idea_draft(
            user_id,
            conversation.id,
            summary=summary,
            mark=mentor_api.IdeaMark.CONSTRAINT,
            mark_note=MARK_NOTE,
        )
        assert draft is not None
        return (await mentor_api.send_idea(user_id, draft.id, summary=summary)).id


async def test_disabled_section_is_404_and_out_of_menu(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id, enabled=False)
    await submit_login(portal.client, portal_hotel.login)

    assert PAGE not in (await portal.client.get(HOME)).text
    for tab in ("", "/mine", "/interview", "/all"):
        assert (await portal.client.get(f"{PAGE}{tab}")).status_code in (403, 404)
    assert (await portal.client.get(PAGE)).status_code == 404
    response = await _post(portal.client, "/turn", {"mode": "idea", "message": "Идея"})
    assert response.status_code == 404


async def test_staff_writes_ideas_but_no_interview_and_no_all_ideas(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF, name="Әсел")

    assert PAGE in (await portal.client.get(HOME)).text
    page = await portal.client.get(PAGE)
    assert page.status_code == 200
    # Читателя-суперадмина в отеле нет — «команда Necturn» без «из команды» (§9).
    assert "Здравствуйте, Әсел!" in page.text
    assert "Её прочитает команда Necturn вместе с вашим именем" in page.text
    assert f"{PAGE}/interview" not in page.text and f"{PAGE}/all" not in page.text
    assert (await portal.client.get(f"{PAGE}/interview")).status_code == 403
    assert (await portal.client.get(f"{PAGE}/all")).status_code == 403
    response = await _post(portal.client, "/turn", {"mode": "interview", "message": "Смена"})
    assert response.status_code == 403
    assert (
        await _post(portal.client, "/turn", {"mode": "idea", "message": " "})
    ).status_code == 422


async def test_manager_without_superadmin_flag_gets_no_all_ideas(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    """§8: руководство отеля карточек не видит — только суперадмин."""
    await _hotel(portal_hotel.tenant_id)
    author = await create_staff_user(
        unique_login(), tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    idea_id = await _sent_idea(portal_hotel.tenant_id, author, SUMMARY)
    await submit_login(portal.client, portal_hotel.login)

    interview = await portal.client.get(f"{PAGE}/interview")
    assert interview.status_code == 200 and "как обычно начинается ваша смена?" in interview.text
    assert f"{PAGE}/all" not in interview.text
    assert (await portal.client.get(f"{PAGE}/all")).status_code == 403
    assert (await _post(portal.client, f"/all/{idea_id}/reviewed")).status_code == 403
    assert (await portal.client.get(f"{API}/export")).status_code == 403


async def test_turn_draft_send_and_mine_never_show_the_mark(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    chat = portal.answer_with(
        MockTurn(text="На первый взгляд хранить карту не получится.", tool_calls=[_card_call()])
    )

    response = await _post(portal.client, "/turn", {"mode": "idea", "message": "Мини-бар"})
    assert response.status_code == 200
    assert "constraint" not in response.text and MARK_NOTE not in response.text
    (_, delta), (kind, done) = _events(response.text)
    assert kind == "done" and delta["text"] == done["answer"]
    assert done["answer"].endswith(IDEA_DRAFT_LABEL)
    assert set(done["draft"]) == {"id", "summary"} and done["draft"]["summary"] == SUMMARY

    page = await portal.client.get(PAGE)
    assert f'data-id="{done["draft"]["id"]}"' in page.text
    assert MARK_NOTE not in page.text

    edited = f"{SUMMARY} И в ресторане тоже."
    sent = await _post(portal.client, "/send", {"idea_id": done["draft"]["id"], "summary": edited})
    assert sent.status_code == 200
    assert len(chat.calls) == 1  # правка и отправка модель не зовут (§3 п. 3)
    again = await _post(portal.client, "/send", {"idea_id": done["draft"]["id"], "summary": edited})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == mentor_api.ERR_MENTOR_NO_IDEA_DRAFT

    mine = await portal.client.get(f"{PAGE}/mine")
    assert edited in mine.text and MARK_NOTE not in mine.text
    assert ideas_all.MARK_TITLES[mentor_api.IdeaMark.CONSTRAINT] not in mine.text
    assert f'data-id="{done["draft"]["id"]}"' not in (await portal.client.get(PAGE)).text

    deleted = await _post(portal.client, f"/mine/{done['draft']['id']}/delete")
    assert deleted.json() == {"deleted": True}
    assert ideas.MINE_EMPTY in (await portal.client.get(f"{PAGE}/mine")).text


async def test_budget_exhausted_has_its_own_text(
    portal: Portal, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_IDEAS_DAILY_BUDGET_USD", "0")
    get_settings.cache_clear()
    await _hotel(portal_hotel.tenant_id)
    await _login(portal.client, portal_hotel, StaffRole.STAFF)
    portal.answer_with()

    response = await _post(portal.client, "/turn", {"mode": "idea", "message": "Идея"})
    [(kind, payload)] = _events(response.text)
    assert (kind, payload["message"]) == ("error", ideas.BUDGET_TEXT)


async def test_reader_is_the_hotel_superadmin(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    await create_superadmin(portal_hotel.tenant_id, unique_login())
    await _login(portal.client, portal_hotel, StaffRole.SENIOR, name="Ерлан")

    page = await portal.client.get(f"{PAGE}/interview")
    assert "Ваши ответы с вашим именем прочитает Основатель из команды Necturn" in page.text


async def test_superadmin_sees_all_ideas_reviews_and_exports_only_own_hotel(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    author = await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Айгерим",
    )
    idea_id = await _sent_idea(portal_hotel.tenant_id, author, SUMMARY)
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other")
        stranger = User(display_name="Чужой")
        session.add_all([other, stranger])
        await session.flush()
        other_id, stranger_id = other.id, stranger.id
    await _sent_idea(other_id, stranger_id, "Идея чужого отеля")
    superadmin_id = await _superadmin(portal.client, portal_hotel)

    with capture_logs() as logs:
        page = await portal.client.get(f"{PAGE}/all")
    assert page.status_code == 200
    for text in ("Айгерим", SUMMARY, MARK_NOTE, "Упирается в ограничение", f"Сказал: {SUMMARY}"):
        assert text in page.text
    assert "Идея чужого отеля" not in page.text
    assert {"event": "superadmin.ideas_viewed", "user_id": str(superadmin_id)}.items() <= next(
        log for log in logs if log["event"] == "superadmin.ideas_viewed"
    ).items()

    assert (await _post(portal.client, f"/all/{idea_id}/reviewed")).json() == {"ok": True}
    assert SUMMARY not in (await portal.client.get(f"{PAGE}/all")).text
    assert SUMMARY in (await portal.client.get(f"{PAGE}/all?show=all")).text

    with capture_logs() as logs:
        export = await portal.client.get(f"{API}/export")
    assert export.status_code == 200
    assert export.headers["content-disposition"].startswith('attachment; filename="ideas-')
    assert SUMMARY in export.text and "Идея чужого отеля" not in export.text
    assert any(
        log["event"] == "superadmin.ideas_exported" and log["user_id"] == str(superadmin_id)
        for log in logs
    )


def _menu_titles() -> set[str]:
    """Названия разделов меню из `shell.nav_items` — разбором исходника, без ролей и флагов."""
    tree = ast.parse(inspect.getsource(shell.nav_items))
    candidates = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "candidates"
    )
    assert isinstance(candidates, ast.List)
    titles: set[str] = set()
    for item in candidates.elts:
        assert isinstance(item, ast.Tuple)
        title = item.elts[3]
        if isinstance(title, ast.Attribute) and isinstance(title.value, ast.Name):
            module = importlib.import_module(f"hospitality.staff_portal.{title.value.id}")
            titles.add(getattr(module, title.attr))
        else:
            assert isinstance(title, ast.Constant) and isinstance(title.value, str)
            titles.add(title.value)
    return titles


def test_capability_map_names_every_menu_section() -> None:
    """§5: новый раздел меню без строки в карте возможностей — красный CI."""
    text = load_prompt("ideas_capability_map")
    titles = _menu_titles()
    assert ideas.NAV_TITLE in titles and len(titles) > 15
    missing = sorted(title for title in titles if f"«{title}»" not in text)
    assert missing == []

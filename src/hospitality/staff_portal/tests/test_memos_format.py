"""«Оформить» и «Шаблоны записок» (spec 0077 §7, §8, §12 PR 6; #936).

«Оформить»: кнопка — только при модели `memos` и отметке согласия на Anthropic, у
объяснительной её нет; действие отдаёт оформленный текст, модели уходят текст и шаблон —
без имени и логина; без отметки — 403 до модели, без модели — ERR-AI-006, раздел выключен —
404. «Шаблоны записок»: вкладка и экран — только менеджеру; «Сохранить» пишет список
целиком, ключ прежнего шаблона сохраняется, новому выдаётся свой, и черновик видит новое.
Провайдер шлюза — Fake-адаптер (ADR-007).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider
from hospitality.app import create_app
from hospitality.platform.config import (
    HotelProfile,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.memo_config import DEFAULT_MEMO_TEMPLATES, MAX_MEMO_TEMPLATES
from hospitality.platform.models import AiRecipient, StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.memos_format_api import get_memo_format_provider
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from tests.conftest import TEST_FILE_ENCRYPTION_KEY, FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
MEMOS = f"{HOME}/memos"
API = f"{HOME}/api/memos"
SAME_ORIGIN = {"origin": "https://test"}
MODEL = "claude-sonnet-5"
TEXT = "в 412 опять течет кран в ванной со вчерашнего вечера, гость ругался"
FORMATTED = "Где: номер 412, ванная комната.\nЧто неисправно: снова течёт кран."
AUTHOR = "Ержан Сантехник"


@pytest.fixture(autouse=True)
def _memos_model() -> Iterator[None]:
    """Модель «Оформить» задана; фейк Redis — бюджеты входа; ключ шифрования — тексты
    записок модуль хранит шифртекстом."""
    fake = FakeRateLimitRedis()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
        patch.setenv("FILE_ENCRYPTION_KEY", TEST_FILE_ENCRYPTION_KEY)
        patch.setenv("LLM_MODEL_MEMOS", MODEL)
        patch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-never-sent")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
async def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client


def _answer_with(app: FastAPI, text: str) -> ScriptedLlmProvider:
    provider = ScriptedLlmProvider([MockTurn(text=text)], model=MODEL)
    app.dependency_overrides[get_memo_format_provider] = lambda: provider
    return provider


async def _hotel(tenant_id: uuid.UUID, *, enabled: bool = True) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                memos_enabled=enabled,
            ),
        )


async def _enter(
    client: AsyncClient,
    hotel: PortalHotel,
    role: StaffRole = StaffRole.STAFF,
    *,
    consents: tuple[AiRecipient, ...] = tuple(AiRecipient),
) -> str:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=hotel.tenant_id,
        role=role,
        display_name=AUTHOR,
        ai_consents=consents,
    )
    response = await client.post(
        f"{HOME}/login", data={"login": staff_login, "password": PASSWORD, "remember": "1"}
    )
    assert response.status_code == 303
    return staff_login


async def _format(client: AsyncClient, **body: Any) -> Response:
    return await client.post(f"{API}/format", json={"text": TEXT, **body}, headers=SAME_ORIGIN)


async def _templates(client: AsyncClient, templates: list[dict[str, Any]]) -> Response:
    return await client.post(f"{API}/templates", json={"templates": templates}, headers=SAME_ORIGIN)


async def test_format_returns_text_and_sends_only_text_and_template(
    app: FastAPI, browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login = await _enter(browser, portal_hotel)
    draft = await browser.get(f"{MEMOS}/new")
    assert draft.status_code == 200
    assert "data-memo-format" in draft.text and texts.FORMAT_RESULT in draft.text
    provider = _answer_with(app, f"\n{FORMATTED}\n")

    response = await _format(browser, template_key="malfunction")

    assert response.status_code == 200, response.text
    assert response.json() == {"text": FORMATTED}
    [request] = provider.calls
    assert request.purpose == "memos"
    [message] = request.messages
    sent = f"{request.system}\n{message.content}"
    malfunction = next(t for t in DEFAULT_MEMO_TEMPLATES if t.key == "malfunction")
    assert TEXT in sent and malfunction.body in sent
    assert AUTHOR not in sent and staff_login not in sent


async def test_no_consent_no_button_and_403_before_the_model(
    app: FastAPI, browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Без отметки согласия на Anthropic (spec 0082) — ни кнопки, ни вызова модели."""
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel, consents=(AiRecipient.OPENAI,))
    provider = _answer_with(app, FORMATTED)

    assert "data-memo-format" not in (await browser.get(f"{MEMOS}/new")).text
    response = await _format(browser)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-003"
    assert provider.calls == []


async def test_no_model_no_button_and_the_action_is_refused(
    app: FastAPI, browser: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel)
    monkeypatch.setenv("LLM_MODEL_MEMOS", "")
    get_settings.cache_clear()
    provider = _answer_with(app, FORMATTED)

    assert "data-memo-format" not in (await browser.get(f"{MEMOS}/new")).text
    response = await _format(browser)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ERR-AI-006"
    assert provider.calls == []


async def test_explanation_draft_has_no_format_button(
    browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Объяснительная — слова работника о проступке: наружу они не уходят (§7)."""
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel)
    page = await browser.get(f"{MEMOS}/explanations/new")
    assert page.status_code == 200
    assert "data-memo-format" not in page.text and texts.FORMAT_BUTTON not in page.text


async def test_format_refuses_disabled_section_and_empty_text(
    browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel)
    assert (await _format(browser, text="  \n ")).status_code == 422

    await _hotel(portal_hotel.tenant_id, enabled=False)
    assert (await _format(browser)).status_code == 404


async def test_templates_are_for_the_manager_only(
    browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel, StaffRole.SENIOR)

    assert texts.TEMPLATES_TAB not in (await browser.get(f"{MEMOS}/mine")).text
    assert (await browser.get(f"{MEMOS}/templates")).status_code == 404
    assert (await _templates(browser, [])).status_code == 403


async def test_manager_saves_templates_and_the_draft_sees_them(
    browser: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel, StaffRole.MANAGER)
    page = await browser.get(f"{MEMOS}/templates")
    assert page.status_code == 200
    assert texts.TEMPLATES_TITLE in page.text and "О закупке" in page.text
    assert texts.TEMPLATES_TAB in (await browser.get(f"{MEMOS}/mine")).text

    response = await _templates(
        browser,
        [
            {"key": "purchase", "title": "Закупка", "body": "Что купить: …"},
            {"key": None, "title": "О премии", "body": "Кому и за что: …"},
        ],
    )

    assert response.status_code == 200, response.text
    keys = response.json()["keys"]
    assert keys[0] == "purchase" and keys[1].startswith("t") and keys[1] != "purchase"
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, portal_hotel.tenant_id)
    assert [(t.key, t.title) for t in config.memo_templates] == [
        ("purchase", "Закупка"),
        (keys[1], "О премии"),
    ]
    draft = (await browser.get(f"{MEMOS}/new")).text
    assert "О премии" in draft and "О неисправности" not in draft

    again = await _templates(
        browser, [{"key": keys[1], "title": "О премии", "body": "Кому и за что: …"}]
    )
    assert again.json()["keys"] == [keys[1]]


@pytest.mark.parametrize(
    "templates",
    [
        [{"title": f"Шаблон {n}", "body": "…"} for n in range(MAX_MEMO_TEMPLATES + 1)],
        [{"title": "  ", "body": "Текст"}],
        [{"title": "Название", "body": ""}],
    ],
    ids=["over-limit", "blank-title", "blank-body"],
)
async def test_invalid_templates_are_rejected_and_nothing_changes(
    browser: AsyncClient, portal_hotel: PortalHotel, templates: list[dict[str, Any]]
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _enter(browser, portal_hotel, StaffRole.MANAGER)

    response = await _templates(browser, templates)

    assert response.status_code == 422
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, portal_hotel.tenant_id)
    assert config.memo_templates == DEFAULT_MEMO_TEMPLATES

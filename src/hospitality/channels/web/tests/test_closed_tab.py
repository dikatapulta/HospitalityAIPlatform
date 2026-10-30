"""Веб-гость узнаёт итог заявки и после закрытия страницы (#223).

Итог («выполнена» / «отменена») подписчик пишет в диалог гостя, push у веба нет.
Поэтому: повторное сканирование листка с того же телефона возвращает в тот же
диалог (cookie сессии лежит и на `/w/{slug}`), а после первой заявки диалога гость
узнаёт, где ждать итог. Черта «Новое» — скрипт страницы, здесь — что он на ней.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.app import create_app
from hospitality.channels.web.request_hint import HINT, hint_text
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

CHAT = f"/g/{HOTEL_SLUG}/{ROOM}"
LINK = f"/w/{HOTEL_SLUG}/b"


@asynccontextmanager
async def _client(provider: ScriptedLlmProvider | None = None) -> AsyncIterator[AsyncClient]:
    app = create_app()
    if provider is not None:
        app.dependency_overrides[get_web_llm_provider] = lambda: provider
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client


def _message(text: str) -> dict[str, str]:
    return {"text": text, "client_message_id": str(uuid.uuid4())}


def _request_call(summary: str) -> MockTurn:
    return MockTurn(
        tool_calls=[
            ToolCall(
                id=f"toolu_{summary}",
                name="create_service_request",
                arguments={
                    "category_key": "housekeeping",
                    "summary": summary,
                    "confirmation_question": "Shall I request it?",
                    "guest_language": "en",
                },
            )
        ]
    )


def _confirm() -> MockTurn:
    return MockTurn(
        tool_calls=[
            ToolCall(
                id="toolu_verdict",
                name="resolve_confirmation",
                arguments={"decision": "confirm", "reply": "Done, passing it on."},
            )
        ]
    )


async def _history(client: AsyncClient) -> list[str]:
    response = await client.get(f"{CHAT}/messages")
    assert response.status_code == 200
    return [message["text"] for message in response.json()["messages"]]


async def test_session_cookie_goes_to_chat_and_link_paths(web_hotel: WebHotel) -> None:
    """Одна cookie на оба адреса канала, атрибуты те же (spec 0027 §3.1)."""
    async with _client() as client:
        response = await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for path, cookie in zip((f"/g/{HOTEL_SLUG}", f"/w/{HOTEL_SLUG}"), cookies, strict=True):
        assert cookie.startswith("guest_session=")
        assert f"Path={path}" in cookie
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie


async def test_rescan_of_the_leaflet_returns_to_the_same_dialog(web_hotel: WebHotel) -> None:
    """Тот же телефон — прежний диалог со всей историей; другой телефон — свой."""
    with tenant_context(web_hotel.tenant_id):
        token = await guests_api.issue_bind_link(web_hotel.stay_id)
    provider = ScriptedLlmProvider([MockTurn(text="Hello! How can I help?")])
    async with _client(provider) as client:
        assert (await client.post(f"{LINK}/{token}/session")).status_code == 200
        assert (await client.post(f"{CHAT}/messages", json=_message("hi"))).status_code == 200

        rescan = await client.post(f"{LINK}/{token}/session")
        assert rescan.status_code == 200
        assert await _history(client) == ["hi", "Hello! How can I help?"]

    async with _client() as other_phone:
        assert (await other_phone.post(f"{LINK}/{token}/session")).status_code == 200
        assert await _history(other_phone) == []


async def test_first_request_tells_the_guest_where_the_outcome_comes(
    web_hotel: WebHotel,
) -> None:
    """Подсказка — сразу после подтверждения, на языке заявки, один раз на диалог."""
    provider = ScriptedLlmProvider(
        [_request_call("towels"), _confirm(), _request_call("water"), _confirm()]
    )
    async with _client(provider) as client:
        await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
        await client.post(f"{CHAT}/messages", json=_message("towels please"))
        first = await client.post(f"{CHAT}/messages", json=_message("yes"))
        await client.post(f"{CHAT}/messages", json=_message("and water"))
        second = await client.post(f"{CHAT}/messages", json=_message("yes"))
        messages = (await client.get(f"{CHAT}/messages")).json()["messages"]

    assert first.json()["replies"] == ["Done, passing it on.", HINT[2]]
    assert second.json()["replies"] == ["Done, passing it on."]
    (hint,) = [message for message in messages if message["text"] == HINT[2]]
    # Курсор опроса — на подсказке: страница не нарисует её второй раз.
    assert first.json()["last_message_id"] == hint["id"]


async def test_hint_failure_does_not_break_the_turn(
    web_hotel: WebHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой подсказки — заявка создана, ответ хода гостю ушёл, 200."""

    async def broken(request_id: uuid.UUID) -> None:
        raise AppError(code="ERR-REQUESTS-002", message="gone", status_code=404)

    # `request_hint` читает заявку через api модуля — подмена там же.
    monkeypatch.setattr(requests_api, "get_request", broken)
    provider = ScriptedLlmProvider([_request_call("towels"), _confirm()])
    async with _client(provider) as client:
        await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
        await client.post(f"{CHAT}/messages", json=_message("towels please"))
        confirmation = await client.post(f"{CHAT}/messages", json=_message("yes"))

    assert confirmation.status_code == 200
    assert confirmation.json()["replies"] == ["Done, passing it on."]
    with tenant_context(web_hotel.tenant_id):
        assert (await requests_api.list_requests(limit=1, offset=0)).total == 1


def test_hint_language_follows_the_request() -> None:
    ru, kk, en = HINT
    assert (hint_text("ru"), hint_text("kk"), hint_text("en")) == (ru, kk, en)
    assert hint_text("de") == en
    assert hint_text(None) == f"{kk}\n{ru}\n{en}"


async def test_chat_page_carries_the_unread_mark(web_hotel: WebHotel) -> None:
    async with _client() as client:
        page = await client.get(CHAT)
    assert page.status_code == 200
    assert "Жаңа · Новое · New" in page.text
    assert "necturn:seen:" in page.text
    assert "__UNREAD_JS__" not in page.text

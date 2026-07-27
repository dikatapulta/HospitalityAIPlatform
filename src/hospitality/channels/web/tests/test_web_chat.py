"""Канал web: auth-only, привязка, сквозной ход, злоупотребления (spec 0027 §5).

DoD issue #79: без валидной привязки — статический ответ БЕЗ вызова LLM и без
заявок; истёкшая сессия не может действовать; комната заявки — из привязки;
перебор кода ограничен по (tenant, room); гостевая сессия бесполезна на
`/api/v1/*` (граница резолвера, ADR-008 §6).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.app import create_app
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.channels.web.tests.conftest import ROOM, WebHotel
from hospitality.modules.guests.api import check_out
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

BASE = f"/g/demo-hotel/{ROOM}"


def _confirm_verdict() -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": "Готово, передаю в службу."},
    )


def _cleaning_call(room: str) -> ToolCall:
    """Модель просит заявку в НАЗВАННУЮ ГОСТЕМ комнату — привязка обязана победить."""
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "уборка",
            "room_number": room,
            "confirmation_question": "Оформить уборку?",
        },
    )


@pytest.fixture
async def stand(
    web_hotel: WebHotel,
) -> AsyncIterator[tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI]]:
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cleaning_call("505")]),
            MockTurn(tool_calls=[_confirm_verdict()]),
        ]
    )
    app = create_app()
    app.dependency_overrides[get_web_llm_provider] = lambda: provider
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client, provider, web_hotel, app


def _message(text: str) -> dict[str, str]:
    return {"text": text, "client_message_id": str(uuid.uuid4())}


async def _bind(client: AsyncClient, code: str) -> None:
    response = await client.post(f"{BASE}/session", json={"code": code})
    assert response.status_code == 200, response.text


async def test_unauthenticated_is_static_and_free(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    """Q7 (решение 22.07): ни LLM-вызова, ни заявки; текст с телефоном ресепшена."""
    client, provider, hotel, _ = stand

    response = await client.post(f"{BASE}/messages", json=_message("уберите номер"))

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "ERR-WEB-002"
    assert "+7 727 000-00-00" in body["error"]["message"]
    assert provider.calls == []  # LLM не вызывался
    listing = await client.get(f"{BASE}/messages")
    assert listing.status_code == 401
    with tenant_context(hotel.tenant_id):
        assert (await requests_api.list_requests(limit=1, offset=0)).total == 0


async def test_bind_chat_and_room_comes_from_binding(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    """Сквозной DoD: код → чат → заявка; «сделайте в 505-й» едет в комнату Stay."""
    client, _provider, hotel, _ = stand
    await _bind(client, f" {hotel.access_code[:3].lower()}-{hotel.access_code[3:].lower()} ")

    proposal = await client.post(f"{BASE}/messages", json=_message("уберите в 505-м"))
    assert proposal.status_code == 200
    assert proposal.json()["replies"] == ["Оформить уборку?"]

    confirmation = await client.post(f"{BASE}/messages", json=_message("да"))
    assert confirmation.status_code == 200
    assert confirmation.json()["replies"] == ["Готово, передаю в службу."]

    with tenant_context(hotel.tenant_id):
        (request,) = (await requests_api.list_requests(limit=10, offset=0)).items
    assert request.room_number == ROOM  # привязка победила текст гостя (issue #79)

    history = await client.get(f"{BASE}/messages")
    texts = [m["text"] for m in history.json()["messages"]]
    assert "уберите в 505-м" in texts and "Оформить уборку?" in texts
    # Poll с курсором: после последнего сообщения нового нет.
    last_id = history.json()["messages"][-1]["id"]
    tail = await client.get(f"{BASE}/messages", params={"after": last_id})
    assert tail.json()["messages"] == []


async def test_duplicate_client_message_id_is_idempotent(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    """Ретрай той же отправки (P-8): второй ход не выполняется."""
    client, provider, hotel, _ = stand
    await _bind(client, hotel.access_code)

    body = _message("уберите номер")
    first = await client.post(f"{BASE}/messages", json=body)
    second = await client.post(f"{BASE}/messages", json=body)

    assert first.json()["duplicate"] is False
    assert second.json() == {"replies": [], "duplicate": True}
    assert len(provider.calls) == 1


async def test_expired_session_cannot_act(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    """Q8 / DoD #79: выезд гасит сессию — та же cookie получает auth-only 401."""
    client, provider, hotel, _ = stand
    await _bind(client, hotel.access_code)
    with tenant_context(hotel.tenant_id):
        await check_out(hotel.stay_id)

    response = await client.post(f"{BASE}/messages", json=_message("ещё воды"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR-WEB-002"
    assert provider.calls == []  # LLM не вызывался
    assert (await client.get(f"{BASE}/messages")).status_code == 401


async def test_code_rate_limit_is_per_room_not_per_client(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 0027 §3.3: лимит по (tenant, room) — второй «клиент» не обнуляет его."""
    client, _provider, hotel, app = stand
    monkeypatch.setenv("GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        assert (await client.post(f"{BASE}/session", json={"code": "WRONG1"})).status_code == 403
        assert (await client.post(f"{BASE}/session", json={"code": "WRONG2"})).status_code == 403
        third = await client.post(f"{BASE}/session", json={"code": "WRONG3"})
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "ERR-WEB-004"
        # «Другое устройство» (свежий клиент без cookie) — тот же лимит комнаты,
        # даже с ВЕРНЫМ кодом: ключ не пер-клиентский.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other:
            blocked = await other.post(f"{BASE}/session", json={"code": hotel.access_code})
        assert blocked.status_code == 429
    finally:
        get_settings.cache_clear()


async def test_guest_session_is_useless_on_service_api_and_vice_versa(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Граница резолвера (ADR-008 §6, замечание ревью ADR): гостевая сессия не
    аутентифицирует staff/API-поверхность, сервисный токен не открывает чат."""
    client, _provider, hotel, app = stand
    await _bind(client, hotel.access_code)
    session_token = client.cookies.get("guest_session")
    assert session_token

    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")
    get_settings.cache_clear()
    try:
        # Гостевой токен как Bearer на /api/v1/* → 401 (резолвер его не знает).
        api_response = await client.get(
            "/api/v1/requests", headers={"Authorization": f"Bearer {session_token}"}
        )
        assert api_response.status_code == 401
        # Сервисный токен не заменяет гостевую сессию: чат без cookie → 401.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as bare:
            chat_response = await bare.post(
                f"{BASE}/messages",
                json=_message("привет"),
                headers={"Authorization": "Bearer test-service-token"},
            )
        assert chat_response.status_code == 401
    finally:
        get_settings.cache_clear()


async def test_unknown_slug_is_404(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    client, _provider, _hotel, _ = stand
    response = await client.get("/g/no-such-hotel/101")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-WEB-001"


async def test_page_serves_html(
    stand: tuple[AsyncClient, ScriptedLlmProvider, WebHotel, FastAPI],
) -> None:
    client, _provider, _hotel, _ = stand
    response = await client.get(BASE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "guest chat" in response.text.lower()

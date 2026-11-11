"""Лента веб-чата без дублей и без потерь (#117).

Страница рисует своё сообщение до ответа сервера, реплики — из ответа отправки,
остальное — из опроса. Опрос приносит и уже показанное: страница узнаёт его по id
реплик из ответа отправки и по `client_message_id` своего входящего, а курсор опроса
двигает только опрос. Прыжок курсора на реплику хода (так было до #117) терял
исходящее подписчика, записанное между входящим и репликой.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import LlmRequest, MockLlmProvider
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.app import create_app
from hospitality.channels.common.models import Message, MessageDirection
from hospitality.channels.common.store import record_outbound_message
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.channels.web.tests.conftest import ROOM, WebHotel
from hospitality.shared.db import session_scope

BASE = f"/g/demo-hotel/{ROOM}"
DONE = "Заявка выполнена"


class _SubscriberMidTurn(MockLlmProvider):
    """Пока модель думает, подписчик пишет в тот же диалог итог заявки."""

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        async with session_scope() as session:
            conversation_id = await session.scalar(
                select(Message.conversation_id).where(Message.direction == MessageDirection.INBOUND)
            )
        assert conversation_id is not None
        await record_outbound_message(
            conversation_id,
            DONE,
            "test",
            external_message_id=None,
            idempotency_key=f"guest:request_done:{uuid.uuid4()}",
        )
        return await super().complete(request)


@pytest.fixture
async def page(web_hotel: WebHotel) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app()
    app.dependency_overrides[get_web_llm_provider] = lambda: _SubscriberMidTurn(text="Ок!")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        bound = await client.post(f"{BASE}/session", json={"code": web_hotel.access_code})
        assert bound.status_code == 200, bound.text
        yield client


async def test_page_recognizes_its_turn_and_still_gets_the_subscriber_message(
    page: AsyncClient,
) -> None:
    cid = str(uuid.uuid4())
    sent = (
        await page.post(f"{BASE}/messages", json={"text": "привет", "client_message_id": cid})
    ).json()
    assert sent["replies"] == ["Ок!"]
    assert len(sent["reply_ids"]) == 1

    # Чат был пуст — курсора у страницы нет, опрос берёт хвост истории.
    history = (await page.get(f"{BASE}/messages")).json()["messages"]
    inbound = [m for m in history if m["direction"] == "inbound"]
    assert [(m["text"], m["client_message_id"]) for m in inbound] == [("привет", cid)]
    assert all(m["client_message_id"] is None for m in history if m["direction"] == "outbound")
    assert {m["id"] for m in history if m["text"] == "Ок!"} == set(sent["reply_ids"])

    # Всё, что страница уже нарисовала, она узнаёт; нового — ровно итог подписчика.
    known = {cid, *sent["reply_ids"]}
    unseen = [
        m["text"] for m in history if m["id"] not in known and m["client_message_id"] not in known
    ]
    assert unseen == [DONE]

    # Курсор из ответа отправки (страница до #117) этот итог пропускал.
    skipped = await page.get(f"{BASE}/messages", params={"after": sent["last_message_id"]})
    assert skipped.json()["messages"] == []

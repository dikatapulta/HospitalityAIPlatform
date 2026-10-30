"""Сбой хода не оставляет веб-гостя без ответа (issue #43).

До страховки упавший ход давал 500: страница писала «не доставлено», хотя
сообщение уже сохранено, а персонал о госте не узнавал. Теперь ответ — 200
с честной репликой того же хода; у отеля без бота и без службы эскалаций
(гостевая копия) это «позвоните на ресепшен» (spec 0051).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.ai import orchestrator
from hospitality.app import create_app
from hospitality.channels.common.guest_turn import no_staff_reply
from hospitality.channels.web.tests.conftest import RECEPTION_PHONE, ROOM, WebHotel
from hospitality.shared.config import get_settings
from tests.conftest import FakeRateLimitRedis

BASE = f"/g/demo-hotel/{ROOM}"


async def test_crashed_turn_answers_in_the_same_response(
    web_hotel: WebHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_limits = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_limits)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "")
    get_settings.cache_clear()

    async def crash(**_: Any) -> None:
        raise RuntimeError("unexpected bug in a tool")

    monkeypatch.setattr(orchestrator, "handle_message", crash)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        bound = await client.post(f"{BASE}/session", json={"code": web_hotel.access_code})
        assert bound.status_code == 200, bound.text

        response = await client.post(
            f"{BASE}/messages",
            json={"text": "уберите номер", "client_message_id": str(uuid.uuid4())},
        )
        history = await client.get(f"{BASE}/messages")
    get_settings.cache_clear()

    expected = no_staff_reply(RECEPTION_PHONE, None)
    assert response.status_code == 200
    assert response.json()["replies"] == [expected]
    # Реплика в истории — страница, открытая заново, её не потеряет.
    assert [m["text"] for m in history.json()["messages"]] == ["уберите номер", expected]

"""Маршрут помощника посетителя `POST /showroom/{slug}/navigate` (spec 0069 §7 п. 3–5).

Допуск — как у сцены: без cookie 403 `005`, витрина не видна снаружи — 404 `001`.
Лимиты по адресу и по отелю — 429 `003`, Redis упал — fail-open. Модель на этом
деплойменте не задана (как в CI) — короткие команды отвечают правила, остальное —
запасной путь «Помощник отдыхает…», а не ошибка. Ход с моделью — `ai/showroom/tests`.
"""

from __future__ import annotations

from httpx import AsyncClient, Response

from hospitality.channels.showroom import navigate
from hospitality.channels.showroom.tests.conftest import (
    CODE,
    SLUG,
    ShowroomHotel,
    set_showroom,
)
from tests.conftest import FakeRateLimitRedis

URL = f"/showroom/{SLUG}/navigate"
RESTING_RU = "Помощник отдыхает — пользуйтесь кнопками этажей и видов."


async def _ask(client: AsyncClient, text: str = "этаж 15", **body: str) -> Response:
    return await client.post(URL, json={"text": text, "language": "ru", **body})


async def _enter(client: AsyncClient) -> None:
    assert (await client.post(f"/showroom/{SLUG}/access", json={"code": CODE})).status_code == 204


async def test_without_access_the_assistant_is_closed(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    answer = await _ask(client)

    assert (answer.status_code, answer.json()["error"]["code"]) == (403, "ERR-SHOWROOM-005")


async def test_short_command_is_answered_by_rules(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)

    answer = await _ask(client, "этаж 15, вид на горы")

    assert answer.status_code == 200
    assert answer.json() == {
        "commands": [
            {"type": "focus_floor", "floor": 15},
            {"type": "set_view", "preset": "mountains"},
        ],
        "reply": "",
        "source": "rules",
    }


async def test_model_off_on_the_deployment_is_the_fallback_not_an_error(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)

    answer = await _ask(client, "Высокий этаж с видом на горы, двенадцать номеров рядом")

    assert answer.status_code == 200
    assert answer.json() == {"commands": [], "reply": RESTING_RU, "source": "fallback"}


async def test_hidden_showroom_is_404_before_access(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await set_showroom(showroom_hotel.tenant_id, enabled=False)

    answer = await _ask(client)

    assert (answer.status_code, answer.json()["error"]["code"]) == (404, "ERR-SHOWROOM-001")


async def test_public_mode_needs_no_cookie(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await set_showroom(showroom_hotel.tenant_id, public_enabled=True)

    assert (await _ask(client)).status_code == 200


async def test_one_address_gets_thirty_phrases_per_ten_minutes(
    client: AsyncClient, showroom_hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    await _enter(client)
    for _ in range(navigate.NAVIGATE_IP_LIMIT):
        assert (await _ask(client)).status_code == 200

    limited = await _ask(client)

    assert (limited.status_code, limited.json()["error"]["code"]) == (429, "ERR-SHOWROOM-003")


async def test_hotel_day_limit_counts_every_address(
    client: AsyncClient, showroom_hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    await _enter(client)
    await _ask(client)
    tenant_key = next(key for key in limits.counters if navigate.NAVIGATE_TENANT_SCOPE in key)
    limits.counters[tenant_key] = navigate.NAVIGATE_TENANT_LIMIT

    limited = await _ask(client)

    assert (limited.status_code, limited.json()["error"]["code"]) == (429, "ERR-SHOWROOM-003")


async def test_redis_down_lets_the_visitor_through(
    client: AsyncClient, showroom_hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    """Fail-open по канону spec 0023: деньги держит бюджет назначения, не лимит."""
    await _enter(client)
    limits.fail = True

    assert (await _ask(client)).status_code == 200


async def test_page_carries_the_assistant_and_its_texts(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)

    page = await client.get(f"/showroom/{SLUG}?lang=ru")

    assert '"navigateUrl": "/showroom/showroom-hotel/navigate"' in page.text
    assert "/showroom/static/assistant.js?v=" in page.text
    assert "Не пишите сюда имена и контакты" in page.text
    assert (await client.get("/showroom/static/assistant.js")).status_code == 200

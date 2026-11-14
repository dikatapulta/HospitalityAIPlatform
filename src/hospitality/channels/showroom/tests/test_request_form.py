"""`POST /showroom/{slug}/requests` и блок форм (spec 0069 §2.4, §4, §7 п. 3, 5–7; PR (в), #805).

Отель — фикстура (а): витрина по коду, сцена — вымышленное здание демо-отеля; посетитель
сначала вводит код (cookie доступа). Службы продаж и бронирования — категории отеля.
Лимиты — на `FakeRateLimitRedis` (канон 0023): в CI живого Redis нет.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from hospitality.channels.showroom import request_form
from hospitality.channels.showroom.consent import SHOWROOM_CONSENT_VERSION
from hospitality.channels.showroom.tests.conftest import CODE, SLUG, ShowroomHotel, set_showroom
from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom.tests.request_fixtures import hotel_today
from hospitality.shared.db import session_scope
from hospitality.shared.errors import MAX_LOC_PART_CHARS, MAX_VALIDATION_DETAILS
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records


@pytest.fixture
async def hotel(showroom_hotel: ShowroomHotel) -> ShowroomHotel:
    """Отель (а) плюс службы витрины: продажи (`sales` — в его конфиге) и бронирование."""
    with tenant_context(showroom_hotel.tenant_id):
        for key, name in (("sales", "Отдел продаж"), ("reservations", "Бронирование")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
    return showroom_hotel


@pytest.fixture
async def visitor(client: AsyncClient, hotel: ShowroomHotel) -> AsyncClient:
    """Посетитель с cookie доступа: ввёл код отеля."""
    entered = await client.post(f"/showroom/{SLUG}/access", json={"code": CODE})
    assert entered.status_code == 204
    return client


def _body(key: str = "key-form-0001", **changes: Any) -> dict[str, Any]:
    today = hotel_today()
    body: dict[str, Any] = {
        "client_key": key,
        "kind": "group",
        "company": "Silk Road Travel",
        "contact_name": "Anna Lee",
        "phone": "+7 701 123 45 67",
        "email": "anna@example.com",
        "guests_count": 32,
        "check_in": (today + timedelta(days=30)).isoformat(),
        "check_out": (today + timedelta(days=33)).isoformat(),
        "room_numbers": ["1501"],
        "language": "en",
    }
    return body | changes


async def _post(client: AsyncClient, body: dict[str, Any], slug: str = SLUG) -> Any:
    return await client.post(f"/showroom/{slug}/requests", json=body)


async def _count(hotel: ShowroomHotel) -> int:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return int(await session.scalar(text("SELECT count(*) FROM showroom_requests")) or 0)


async def _category_id(hotel: ShowroomHotel, key: str) -> object:
    with tenant_context(hotel.tenant_id):
        categories = await requests_api.list_categories()
    return next(category.id for category in categories if category.key == key)


async def _consent_version(hotel: ShowroomHotel) -> str | None:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            version = await session.scalar(text("SELECT consent_version FROM showroom_requests"))
    return str(version) if version is not None else None


async def test_new_request_is_201_and_repeat_is_200(
    visitor: AsyncClient, hotel: ShowroomHotel
) -> None:
    first = await _post(visitor, _body())
    again = await _post(visitor, _body())

    assert first.status_code == 201
    assert first.json() == {"status": "sent"}
    assert again.status_code == 200
    assert await _count(hotel) == 1
    with tenant_context(hotel.tenant_id):
        [request] = await requests_api.list_open_requests(limit=5)
    assert await _consent_version(hotel) == SHOWROOM_CONSENT_VERSION
    assert request.category_id == await _category_id(hotel, "sales")


async def test_without_access_cookie_is_403_005(client: AsyncClient, hotel: ShowroomHotel) -> None:
    response = await _post(client, _body())

    assert (response.status_code, response.json()["error"]["code"]) == (403, "ERR-SHOWROOM-005")
    assert await _count(hotel) == 0


async def test_page_carries_form_block_only_with_access(
    client: AsyncClient, hotel: ShowroomHotel
) -> None:
    locked = await client.get(f"/showroom/{SLUG}?lang=ru")
    await client.post(f"/showroom/{SLUG}/access", json={"code": CODE})
    page = await client.get(f"/showroom/{SLUG}?lang=ru")

    assert "showroom-request-data" not in locked.text
    assert 'id="showroom-request"' in page.text
    assert "/showroom/static/request_form.js?v=" in page.text
    assert (await client.get("/showroom/static/request_form.js")).status_code == 200


async def test_trap_field_answers_200_and_writes_nothing(
    visitor: AsyncClient, hotel: ShowroomHotel
) -> None:
    response = await _post(visitor, _body(website="http://spam.example"))

    assert response.status_code == 200
    assert await _count(hotel) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"phone": None, "email": None},
        {"guests_count": 0},
        {"room_numbers": ["9999"]},
        {"wish_view_key": "sea"},
        {"check_in": "2000-01-01", "check_out": "2000-01-02"},
        {"unexpected": "field"},
    ],
)
async def test_out_of_rules_is_422_004_without_values(
    visitor: AsyncClient, hotel: ShowroomHotel, changes: dict[str, Any]
) -> None:
    response = await _post(visitor, _body(**changes))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-SHOWROOM-004"
    assert "Anna" not in response.text and "anna@example.com" not in response.text
    assert await _count(hotel) == 0


async def test_thousands_of_extra_keys_give_short_message_and_log(
    visitor: AsyncClient, hotel: ShowroomHotel, capsys: pytest.CaptureFixture[str]
) -> None:
    """#968: имена лишних ключей — ввод посетителя: в `message` и лог — 20 путей, усечённых."""
    configure_logging()
    body = _body() | {f"k{number}" + "x" * 200: 0 for number in range(3_000)}

    response = await _post(visitor, body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "ERR-SHOWROOM-004")
    assert len(response.content) < 3_000
    record = find_record(read_log_records(capsys), "showroom_request_invalid")
    assert record["error_count"] == 3_000 and len(record["fields"]) == MAX_VALIDATION_DETAILS
    assert all(len(field) <= MAX_LOC_PART_CHARS for field in record["fields"])
    assert await _count(hotel) == 0


async def test_disabled_showroom_or_unknown_hotel_is_404_001(
    visitor: AsyncClient, hotel: ShowroomHotel
) -> None:
    await set_showroom(hotel.tenant_id, enabled=False)

    disabled = await _post(visitor, _body())
    unknown = await _post(visitor, _body(), slug="no-such-hotel")

    assert (disabled.status_code, disabled.json()["error"]["code"]) == (404, "ERR-SHOWROOM-001")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "ERR-SHOWROOM-001")


async def test_address_limit_is_429_003(visitor: AsyncClient, hotel: ShowroomHotel) -> None:
    for number in range(5):
        assert (await _post(visitor, _body(key=f"key-ip-{number:04d}"))).status_code == 201

    limited = await _post(visitor, _body(key="key-ip-0005"))

    assert (limited.status_code, limited.json()["error"]["code"]) == (429, "ERR-SHOWROOM-003")
    assert await _count(hotel) == 5


async def test_hotel_limit_is_429_003(visitor: AsyncClient, hotel: ShowroomHotel) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(request_form, "_IP_LIMIT", 100)
        patch.setattr(request_form, "_TENANT_LIMIT", 2)
        for number in range(2):
            assert (await _post(visitor, _body(key=f"key-th-{number:04d}"))).status_code == 201
        limited = await _post(visitor, _body(key="key-th-0002"))

    assert (limited.status_code, limited.json()["error"]["code"]) == (429, "ERR-SHOWROOM-003")


async def test_limits_fail_open_when_redis_is_down(
    visitor: AsyncClient, hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    limits.fail = True

    assert (await _post(visitor, _body())).status_code == 201


def test_form_block_carries_texts_and_limits() -> None:
    block = request_form.form_block(
        tenant_slug="hotel</script>", today=date(2026, 11, 5), booking_max_guests=6
    )

    payload = re.search(r'id="showroom-request-data">(.*?)</script>', block, re.S)
    assert payload is not None
    data = json.loads(payload.group(1))
    assert data["slug"] == "hotel</script>"
    assert (data["today"], data["max_booking_guests"], data["max_days_ahead"]) == (
        "2026-11-05",
        6,
        400,
    )
    assert data["texts"]["ru"]["group_title"] == "Запрос в отдел продаж"
    assert data["texts"]["en"]["group_title"] == "Request to the sales team"
    assert 'href="/legal/showroom-privacy"' in data["texts"]["kk"]["consent_html"]

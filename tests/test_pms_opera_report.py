"""Приём выгрузок Opera маршрутами загрузки и токен загрузки.

Снимок «кто в доме» — spec 0058 §2.4, §5 п. 5; свободные номера по ночам —
spec 0059 §2.1, §5 п. 2 (свой маршрут, свой счётчик частоты, снимок не трогает).

Канон HTTP-теста с БД — `httpx.AsyncClient` поверх настоящего `create_app` с
`PMS_BACKEND=opera_report`. Тест на верхнем уровне, как контрактный
`test_pms_port.py`: адаптер живёт в `integrations/` (контракт 2 import-linter).
Разбор файла по причинам — `test_pms_opera_parse.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from hospitality.app import create_app
from hospitality.integrations.pms import OperaReportPmsAdapter, issue_upload_token
from hospitality.integrations.pms.opera_report import adapter as adapter_module
from hospitality.modules.guests import api as guests_api
from hospitality.modules.guests.models import Stay
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.ports.pms import PmsAvailability, PmsSnapshot, pms_or_none, set_pms
from hospitality.shared.tenancy import tenant_context
from hospitality.tools import pms_token
from tests.conftest import FakeRateLimitRedis

HEADER = "room,confirmation_no,arrival,departure,guests"
URL = "/integrations/pms/{slug}/opera-report"
AVAILABILITY_URL = "/integrations/pms/{slug}/opera-availability"


def report(*lines: str, header: str = HEADER) -> bytes:
    return ("\n".join([header, *lines]) + "\n").encode()


GOOD = report("305,123456,2026-11-02,2026-11-04,2", "410,654321,2026-11-01,2026-11-06,")
FREE_ROOMS = report("2026-11-02,12", "2026-11-03,0", header="date,free_rooms")


async def add_hotel(slug: str, *, pms_source: str = "opera_report") -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "pms_source": pms_source,
                }
            ),
        )
        return tenant.id


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> FakeRateLimitRedis:
    fake = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    return fake


@pytest.fixture
async def client(
    canonical_database: None, limits: FakeRateLimitRedis, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("PMS_BACKEND", "opera_report")
    get_settings.cache_clear()
    previous = pms_or_none()
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        get_settings.cache_clear()
        if previous is not None:
            set_pms(previous)


@pytest.fixture
async def hotel(client: AsyncClient) -> tuple[uuid.UUID, str]:
    """Отель со сверкой и выпущенным токеном: (id, токен)."""
    tenant_id = await add_hotel("hotel-opera")
    return tenant_id, await issue_upload_token(tenant_id)


async def upload(
    client: AsyncClient, token: str | None, body: bytes = GOOD, slug: str = "hotel-opera"
) -> Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.post(URL.format(slug=slug), content=body, headers=headers)


async def upload_availability(
    client: AsyncClient, token: str, body: bytes = FREE_ROOMS, slug: str = "hotel-opera"
) -> Response:
    headers = {"Authorization": f"Bearer {token}"}
    return await client.post(AVAILABILITY_URL.format(slug=slug), content=body, headers=headers)


async def snapshot(tenant_id: uuid.UUID) -> PmsSnapshot | None:
    return await OperaReportPmsAdapter(max_guests=20).fetch_snapshot(tenant_id)


async def free_rooms(tenant_id: uuid.UUID) -> PmsAvailability | None:
    return await OperaReportPmsAdapter(max_guests=20).fetch_availability(tenant_id)


def error_code(response: Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


async def test_accepted_file_becomes_the_snapshot(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, token = hotel
    response = await upload(client, token)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "rows": 2}
    taken = await snapshot(tenant_id)
    assert taken is not None
    assert {(row.room_number, row.booking_ref, row.guests_count) for row in taken.rows} == {
        ("305", "123456", 2),
        ("410", "654321", None),
    }


async def test_same_file_within_a_minute_is_a_repeat_not_a_snapshot(
    client: AsyncClient, hotel: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, token = hotel
    await upload(client, token)
    first = await snapshot(tenant_id)
    assert first is not None

    repeat = await upload(client, token)
    assert repeat.status_code == 200
    assert repeat.json() == {"accepted": False, "duplicate": True}
    assert (await snapshot(tenant_id)) == first

    later = utc_now() + timedelta(seconds=61)
    monkeypatch.setattr(adapter_module, "utc_now", lambda: later)
    assert (await upload(client, token)).json() == {"accepted": True, "rows": 2}
    second = await snapshot(tenant_id)
    assert second is not None
    assert second.source_id != first.source_id
    assert second.rows == first.rows


@pytest.mark.parametrize(
    "body",
    [
        report("305,123456,2026-11-02,2026-11-04,2").replace(b"guests", b"name"),
        report("305,123456,2026-11-04,2026-11-02,2"),
        b"\xff\xfe",
    ],
)
async def test_rejected_file_leaves_the_snapshot_alone(
    client: AsyncClient, hotel: tuple[uuid.UUID, str], body: bytes
) -> None:
    tenant_id, token = hotel
    await upload(client, token)
    before = await snapshot(tenant_id)

    response = await upload(client, token, body)

    assert response.status_code == 400
    assert error_code(response) == "ERR-PMS-001"
    assert "reason:" in response.json()["error"]["message"]
    assert (await snapshot(tenant_id)) == before


async def test_wrong_or_missing_token_is_401(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, token = hotel
    other_hotel = await add_hotel("hotel-other")
    other_token = await issue_upload_token(other_hotel)

    for response in [
        await upload(client, None),
        await upload(client, "not-the-token"),
        await upload(client, other_token),  # токен другого отеля
        await upload(client, token, slug="no-such-hotel"),
        await client.post(
            URL.format(slug="hotel-opera"), content=GOOD, headers={"Authorization": token}
        ),
    ]:
        assert response.status_code == 401
        assert error_code(response) == "ERR-PMS-002"
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert await snapshot(tenant_id) is None


async def test_reissued_token_retires_the_old_one(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, old = hotel
    await upload(client, old)
    new = await issue_upload_token(tenant_id)

    assert (await upload(client, old)).status_code == 401
    assert (await upload(client, new, report("305,1,2026-11-02,2026-11-03,1"))).status_code == 200
    assert await snapshot(tenant_id) is not None


async def test_file_over_one_megabyte_is_413(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, token = hotel
    response = await upload(client, token, GOOD + b"\n" * (1024 * 1024))
    assert response.status_code == 413
    assert error_code(response) == "ERR-PMS-003"
    assert await snapshot(tenant_id) is None


async def test_more_than_30_uploads_an_hour_is_429_and_strangers_do_not_count(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    _, token = hotel
    for _ in range(10):
        assert (await upload(client, "stranger")).status_code == 401
    for _ in range(30):
        assert (await upload(client, token)).status_code == 200
    response = await upload(client, token)
    assert response.status_code == 429
    assert error_code(response) == "ERR-PMS-004"


async def test_hotel_without_opera_switched_on_gets_409_and_nothing_is_stored(
    client: AsyncClient,
) -> None:
    """Токен выпущен заранее, а приём не включён: номера броней в базу не ложатся (§4)."""
    tenant_id = await add_hotel("hotel-manual", pms_source="manual")
    token = await issue_upload_token(tenant_id)

    response = await upload(client, token, slug="hotel-manual")

    assert response.status_code == 409
    assert error_code(response) == "ERR-PMS-005"
    assert await snapshot(tenant_id) is None


async def test_logs_carry_no_booking_numbers_or_rooms(
    client: AsyncClient, hotel: tuple[uuid.UUID, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Латинские маркеры: JSON-лог экранирует кириллицу, и русский номер брони тест бы не нашёл."""
    _, token = hotel
    caplog.set_level(logging.INFO)
    accepted = report("ROOMMARKERQZ,BOOKMARKERQZ,2026-11-02,2026-11-04,2")
    rejected = report("ROOMMARKERQZ,BOOKMARKERQZ,2026-11-04,2026-11-02,2")
    assert (await upload(client, token, accepted)).status_code == 200
    assert (await upload(client, token, rejected)).status_code == 400

    assert "pms_report_uploaded" in caplog.text
    assert "pms_report_rejected" in caplog.text
    assert "MARKERQZ" not in caplog.text


async def test_fake_backend_has_no_upload_route(canonical_database: None) -> None:
    get_settings.cache_clear()
    previous = pms_or_none()
    try:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            responses = [
                await http.post(url.format(slug="hotel-opera"), content=GOOD)
                for url in (URL, AVAILABILITY_URL)
            ]
    finally:
        if previous is not None:
            set_pms(previous)
    assert [response.status_code for response in responses] == [404, 404]


# --- Свободные номера по ночам (spec 0059 §2.1) ------------------------------


async def test_availability_file_is_stored_beside_the_snapshot(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, token = hotel
    await upload(client, token)
    before = await snapshot(tenant_id)

    response = await upload_availability(client, token)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "nights": 2}
    stored = await free_rooms(tenant_id)
    assert stored is not None
    assert [(night.night, night.free_rooms) for night in stored.nights] == [
        (date(2026, 11, 2), 12),
        (date(2026, 11, 3), 0),
    ]
    assert await snapshot(tenant_id) == before


async def test_same_availability_file_within_a_minute_is_a_repeat(
    client: AsyncClient, hotel: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, token = hotel
    await upload_availability(client, token)
    first = await free_rooms(tenant_id)
    assert first is not None

    repeat = await upload_availability(client, token)
    assert repeat.json() == {"accepted": False, "duplicate": True}
    assert await free_rooms(tenant_id) == first

    later = utc_now() + timedelta(seconds=61)
    monkeypatch.setattr(adapter_module, "utc_now", lambda: later)
    assert (await upload_availability(client, token)).json() == {"accepted": True, "nights": 2}
    second = await free_rooms(tenant_id)
    assert second is not None
    assert second.taken_at > first.taken_at


async def test_rejected_availability_file_keeps_the_previous_one(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, token = hotel
    await upload_availability(client, token)
    before = await free_rooms(tenant_id)

    gap = report("2026-11-02,4", "2026-11-04,5", header="date,free_rooms")
    response = await upload_availability(client, token, gap)

    assert response.status_code == 400
    assert error_code(response) == "ERR-PMS-001"
    assert "free-room figures unchanged" in response.json()["error"]["message"]
    assert "reason: night_gap" in response.json()["error"]["message"]
    assert await free_rooms(tenant_id) == before


async def test_availability_needs_the_token_and_opera_switched_on(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    tenant_id, _ = hotel
    manual = await add_hotel("hotel-manual", pms_source="manual")

    unauthorized = await upload_availability(client, "not-the-token")
    disabled = await upload_availability(
        client, await issue_upload_token(manual), slug="hotel-manual"
    )

    assert (unauthorized.status_code, error_code(unauthorized)) == (401, "ERR-PMS-002")
    assert (disabled.status_code, error_code(disabled)) == (409, "ERR-PMS-005")
    assert await free_rooms(tenant_id) is None
    assert await free_rooms(manual) is None


async def test_availability_has_its_own_hourly_limit(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    """Частый второй файл не съедает 30 файлов в час снимка «в доме» (spec 0059 §2.1)."""
    _, token = hotel
    for _ in range(30):
        assert (await upload_availability(client, token)).status_code == 200
    response = await upload_availability(client, token)

    assert (response.status_code, error_code(response)) == (429, "ERR-PMS-004")
    assert (await upload(client, token)).status_code == 200


async def test_uploaded_file_checks_guests_in_through_the_sync(
    client: AsyncClient, hotel: tuple[uuid.UUID, str]
) -> None:
    """Сквозной путь: файл отеля → снимок адаптера → шаг воркера заводит Stay из Opera."""
    tenant_id, token = hotel
    today = utc_now().date()
    line = f"305,123456,{today},{today + timedelta(days=2)},2"
    assert (await upload(client, token, report(line))).status_code == 200

    assert await guests_api.sync_stays_from_pms() == 1
    with tenant_context(tenant_id):
        async with session_scope() as session:
            stay = (await session.scalars(select(Stay))).one()
    assert (stay.room_number, stay.pms_booking_ref, stay.guests_count) == ("305", "123456", 2)


async def test_token_tool_issues_a_working_token(client: AsyncClient) -> None:
    tenant_id = await add_hotel("hotel-tool")

    token = await pms_token.issue_for_slug("hotel-tool")

    assert (await upload(client, token, slug="hotel-tool")).status_code == 200
    assert await snapshot(tenant_id) is not None
    with pytest.raises(pms_token.PmsTokenError, match="не найден"):
        await pms_token.issue_for_slug("no-such-hotel")


def test_token_tool_prints_the_token_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake(tenant_slug: str) -> str:
        if tenant_slug != "hotel-tool":
            raise pms_token.PmsTokenError("Отель с кодом 'x' не найден.")
        return "TOKENMARKERQZ"

    monkeypatch.setattr(pms_token, "issue_for_slug", fake)

    assert pms_token.main(["hotel-tool"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "TOKENMARKERQZ"
    assert pms_token.main(["other"]) == 1
    assert "не найден" in capsys.readouterr().err

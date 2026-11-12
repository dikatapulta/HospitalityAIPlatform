"""Забытые заявки — снимок для алертера (issue #1050, часть 4; ERR-OPS-025).

Сроки команды — spec 0018 §3, п. 8а: срочная и эскалация — 10 минут, остальные —
три срока службы. Время сдвигается подменой `utc_now` модуля, а не правкой
`created_at`: заявки заводятся настоящим `create_request`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.metrics import ForgottenRequests, set_forgotten_requests
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import unclaimed_watch
from hospitality.staff_portal.tests.conftest import PortalHotel

Origin = requests_api.ServiceRequestOrigin


@pytest.fixture(autouse=True)
def _clean_metric() -> Iterator[None]:
    set_forgotten_requests({})
    yield
    set_forgotten_requests({})


def _sample(name: str, tenant_id: uuid.UUID, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"tenant_id": str(tenant_id), **labels})


def _forgotten(tenant_id: uuid.UUID) -> tuple[float | None, float | None, float | None]:
    return (
        _sample("service_requests_forgotten", tenant_id, kind="urgent"),
        _sample("service_requests_forgotten", tenant_id, kind="regular"),
        _sample("service_requests_forgotten_oldest_age_seconds", tenant_id),
    )


async def _configure(tenant_id: uuid.UUID, *, reminder_minutes: int | None = 30) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                request_reminder_after_minutes=reminder_minutes,
                request_reminder_minutes_by_category={"reception": 10},
            ),
        )


async def _category(tenant_id: uuid.UUID, key: str) -> uuid.UUID:
    with tenant_context(tenant_id):
        created = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name=key)
        )
    return created.id


async def _request(
    tenant_id: uuid.UUID,
    category_id: uuid.UUID,
    *,
    urgent: bool = False,
    origin: requests_api.ServiceRequestOrigin = Origin.STAFF_MANUAL,
) -> uuid.UUID:
    with tenant_context(tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=origin,
                summary="Принести полотенца",
                room_number="305",
                is_urgent=urgent,
            )
        )
    return created.id


def _later(monkeypatch: pytest.MonkeyPatch, minutes: float) -> datetime:
    moment = utc_now() + timedelta(minutes=minutes)
    monkeypatch.setattr(unclaimed_watch, "utc_now", lambda: moment)
    return moment


@pytest.fixture
async def hotel(portal_hotel: PortalHotel) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Отель пилота: «Ресепшен» со сроком 10 мин, прачечная — по базовому 30 мин."""
    await _configure(portal_hotel.tenant_id)
    reception = await _category(portal_hotel.tenant_id, "reception")
    laundry = await _category(portal_hotel.tenant_id, "laundry")
    return portal_hotel.tenant_id, reception, laundry


async def test_urgent_and_escalation_after_ten_minutes_regular_after_three_terms(
    hotel: tuple[uuid.UUID, uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, reception, laundry = hotel
    await _request(tenant_id, laundry, urgent=True)
    await _request(tenant_id, laundry, origin=Origin.ESCALATION)
    await _request(tenant_id, reception)  # 3 × 10 мин
    await _request(tenant_id, laundry)  # 3 × 30 мин

    _later(monkeypatch, 9)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id) == (0.0, 0.0, 0.0)

    _later(monkeypatch, 11)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    urgent, regular, oldest = _forgotten(tenant_id)
    assert (urgent, regular) == (2.0, 0.0)
    assert oldest is not None and 11 * 60 <= oldest < 12 * 60

    _later(monkeypatch, 31)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id)[:2] == (2.0, 1.0)

    _later(monkeypatch, 91)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id)[:2] == (2.0, 2.0)


async def test_taken_request_is_not_forgotten(
    hotel: tuple[uuid.UUID, uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Невзятая» — ровно `new`: взятая в работу команду не зовёт."""
    tenant_id, reception, _laundry = hotel
    request_id = await _request(tenant_id, reception, urgent=True)
    with tenant_context(tenant_id):
        await requests_api.change_request_status(request_id, requests_api.RequestStatus.IN_PROGRESS)

    _later(monkeypatch, 60)
    await unclaimed_watch.refresh_forgotten_request_metrics()

    assert _forgotten(tenant_id) == (0.0, 0.0, 0.0)


async def test_switched_off_reminders_still_have_the_team_term(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Напоминания выключены — снимаются ступени кабинета, не страховка команды:
    срок — три срока по умолчанию платформы (30 мин)."""
    tenant_id = portal_hotel.tenant_id
    await _configure(tenant_id, reminder_minutes=None)
    laundry = await _category(tenant_id, "laundry")
    await _request(tenant_id, laundry)

    _later(monkeypatch, 89)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id)[1] == 0.0

    _later(monkeypatch, 91)
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id)[1] == 1.0


async def test_tenant_without_config_is_skipped_and_other_tenants_are_isolated(
    hotel: tuple[uuid.UUID, uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тенант без конфига — не в снимке (онбординг, smoke); заявки другого тенанта
    не видны (RLS, P-4)."""
    tenant_id, _reception, laundry = hotel
    async with platform_session_scope() as session:
        bare = Tenant(slug=f"bare-{uuid.uuid4().hex[:8]}", name="Bare")
        session.add(bare)
        await session.flush()
        bare_id = bare.id
    await _request(tenant_id, laundry, urgent=True)

    _later(monkeypatch, 15)
    await unclaimed_watch.refresh_forgotten_request_metrics()

    assert _forgotten(tenant_id)[0] == 1.0
    assert _forgotten(bare_id) == (None, None, None)


async def test_failing_tenant_leaves_only_itself_out(
    hotel: tuple[uuid.UUID, uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, _reception, _laundry = hotel

    async def broken(_tenant_id: uuid.UUID, _now: datetime) -> ForgottenRequests:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(unclaimed_watch, "_tenant_forgotten", broken)

    await unclaimed_watch.refresh_forgotten_request_metrics()  # не бросает

    assert _forgotten(tenant_id) == (None, None, None)


async def test_unreadable_database_clears_the_snapshot(
    hotel: tuple[uuid.UUID, uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """БД не ответила — снимок стёрт: для алертера это «не знаю», а не нули."""
    tenant_id, _reception, _laundry = hotel
    await unclaimed_watch.refresh_forgotten_request_metrics()
    assert _forgotten(tenant_id) == (0.0, 0.0, 0.0)

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[None]:
        raise OSError("connection refused")
        yield

    monkeypatch.setattr(unclaimed_watch, "platform_session_scope", broken_scope)

    await unclaimed_watch.refresh_forgotten_request_metrics()

    assert _forgotten(tenant_id) == (None, None, None)

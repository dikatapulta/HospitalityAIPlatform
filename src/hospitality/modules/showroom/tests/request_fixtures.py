"""Отель, витрина и сцена для тестов запросов (spec 0069 §7 п. 6).

Сцена подменяется на уровне `load_scene_facts`: запрос берёт из неё только номера,
виды и типы (`request_scene.py`), хранение сцены — PR (а).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom import request_scene, visitor_requests
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

ZONE = ZoneInfo("Asia/Almaty")
SCENE = request_scene.SceneFacts(
    room_numbers=frozenset({"1901", "1903", "1905", "2001"}),
    views={"mountains": LocalizedText(en="Mountains", ru="Горы", kk="Таулар")},
    room_types={"deluxe": LocalizedText(en="Deluxe", ru="Делюкс")},
)


def showroom_config(**changes: Any) -> ShowroomConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "sales_category_key": "sales",
        "booking_category_key": "reservations",
    }
    return ShowroomConfig(**(values | changes))


async def configure(tenant_id: uuid.UUID, showroom: ShowroomConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                showroom=showroom,
            ),
        )


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    sales_id: uuid.UUID
    reservations_id: uuid.UUID


async def make_hotel(slug: str) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    await configure(tenant_id, showroom_config())
    with tenant_context(tenant_id):
        sales = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="sales", name="Продажи")
        )
        reservations = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reservations", name="Бронирование")
        )
    return Hotel(tenant_id=tenant_id, sales_id=sales.id, reservations_id=reservations.id)


@pytest.fixture
def scene() -> Iterator[request_scene.SceneFacts]:
    """Своя MonkeyPatch, не общая `monkeypatch`: фикстура стоит в `usefixtures` раньше
    `canonical_database`, и общий откат вернул бы POSTGRES_DB до удаления временной
    БД — DROP из неё самой (канон `tests/conftest.py`, заморозка лимитов)."""

    async def load() -> request_scene.SceneFacts:
        return SCENE

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(visitor_requests, "load_scene_facts", load)
        yield SCENE


def hotel_today() -> date:
    return utc_now().astimezone(ZONE).date()


def group(key: str = "key-group-1", **changes: Any) -> showroom_api.ShowroomRequestCreate:
    today = hotel_today()
    values: dict[str, Any] = {
        "client_key": key,
        "kind": "group",
        "company": "Silk Road Travel",
        "contact_name": "Anna Lee",
        "phone": "+7 701 123 45 67",
        "email": "anna@example.com",
        "guests_count": 32,
        "check_in": today + timedelta(days=30),
        "check_out": today + timedelta(days=33),
        "room_numbers": ["1901", "1903"],
        "wishes": "Breakfast at 7",
        "language": "en",
    }
    return showroom_api.ShowroomRequestCreate.model_validate(values | changes)


def booking(key: str = "key-book-1", **changes: Any) -> showroom_api.ShowroomRequestCreate:
    today = hotel_today()
    values: dict[str, Any] = {
        "client_key": key,
        "kind": "booking",
        "contact_name": "Ержан",
        "phone": "+7 777 000 11 22",
        "guests_count": 2,
        "check_in": today + timedelta(days=10),
        "check_out": today + timedelta(days=12),
        "wish_view_key": "mountains",
        "language": "ru",
    }
    return showroom_api.ShowroomRequestCreate.model_validate(values | changes)


async def send(
    hotel: Hotel, data: showroom_api.ShowroomRequestCreate
) -> showroom_api.ShowroomRequestPlaced:
    with tenant_context(hotel.tenant_id):
        return await showroom_api.create_request(data, consent_version="2026-11-04-v1")

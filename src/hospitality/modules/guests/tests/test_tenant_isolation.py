"""Изоляция тенантов на таблицах модуля guests (spec 0027 §5 тест 1, P-4, ADR-003).

Канон — `tests/test_tenant_isolation.py` (канарейка); здесь те же проверки на
настоящих гостевых таблицах: RLS-видимость, WITH CHECK на подлог tenant_id,
недоступность чужого Stay/сессии на уровне сервисных функций.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from hospitality.modules.guests.api import (
    GuestSessionStart,
    find_active_stay,
    resolve_session,
    start_guest_session,
)
from hospitality.modules.guests.models import Guest, GuestSession, Stay, StayAccessCode
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import tenant_context


def _bind(code: str, room: str = "101") -> GuestSessionStart:
    return GuestSessionStart(
        room_number=room,
        code=code,
        identity_external_id=str(uuid.uuid4()),
        consent_version="v1",
    )


async def test_tenant_sees_only_own_stays(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    await check_in_room(tenant_a, "101")

    with tenant_context(tenant_b):
        assert await find_active_stay("101") is None
        async with session_scope() as session:
            assert (await session.scalar(select(func.count()).select_from(Stay))) == 0

    # Та же комната у другого тенанта — не конфликт: partial unique включает tenant_id.
    await check_in_room(tenant_b, "101")
    with tenant_context(tenant_a):
        stay_a = await find_active_stay("101")
        assert stay_a is not None


async def test_insert_with_foreign_tenant_id_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """WITH CHECK политики: подлог чужого tenant_id отвергает БД, не дисциплина."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(Guest(tenant_id=tenant_b, display_name="stolen"))
            await session.flush()


async def test_code_and_session_are_tenant_scoped(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Код и токен тенанта A бесполезны в контексте тенанта B (тройка + RLS)."""
    tenant_a, tenant_b = two_tenants
    result = await check_in_room(tenant_a, "101")
    await check_in_room(tenant_b, "101")

    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None
    with tenant_context(tenant_b):
        # Код A не привязывает к Stay B той же комнаты…
        assert await start_guest_session(_bind(result.access_code)) is None
        # …а сессия A не резолвится в контексте B.
        assert await resolve_session(grant.session_token) is None


async def test_platform_scope_cannot_read_guest_tables(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Платформенная сессия (без контекста тенанта) не видит гостевых таблиц."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session(_bind(result.access_code)) is not None

    async with platform_session_scope() as session:
        for table in (Guest, Stay, StayAccessCode, GuestSession):
            assert (await session.scalar(select(func.count()).select_from(table))) == 0

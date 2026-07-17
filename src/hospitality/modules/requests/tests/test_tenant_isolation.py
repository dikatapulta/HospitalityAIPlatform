"""Изоляция тенантов на таблицах модуля requests (Task 0012, P-4, ADR-003).

Карточка задачи требует тест изоляции на КАЖДОЙ новой тенантной таблице:
`request_categories` и `service_requests`. Вечный якорь канона —
`tests/test_tenant_isolation.py` (канарейка, отдельный шаг CI); здесь —
те же проверки на настоящих бизнес-таблицах и, дополнительно, изоляция
на уровне сервисных функций.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    RequestStatus,
    ServiceRequestCreate,
    change_request_status,
    create_request,
)
from hospitality.modules.requests.models import RequestCategory, ServiceRequest
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def _visible_category_keys() -> set[str]:
    async with session_scope() as session:
        rows = (await session.execute(select(RequestCategory.key))).scalars().all()
    return set(rows)


async def _visible_request_summaries() -> set[str]:
    async with session_scope() as session:
        rows = (await session.execute(select(ServiceRequest.summary))).scalars().all()
    return set(rows)


@pytest.fixture
async def request_in_each_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> tuple[uuid.UUID, uuid.UUID]:
    """По категории и заявке у каждого тенанта; возвращает id заявок (A, B)."""
    tenant_a, tenant_b = two_tenants
    category_a = await make_category(tenant_a)
    category_b = await make_category(tenant_b)
    with tenant_context(tenant_a):
        request_a = await create_request(
            ServiceRequestCreate(category_id=category_a.id, summary="a-request")
        )
    with tenant_context(tenant_b):
        request_b = await create_request(
            ServiceRequestCreate(category_id=category_b.id, summary="b-request")
        )
    return (request_a.id, request_b.id)


async def test_tenant_sees_only_own_rows_in_both_tables(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    request_in_each_tenant: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        assert await _visible_category_keys() == {"housekeeping"}
        assert await _visible_request_summaries() == {"a-request"}
    with tenant_context(tenant_b):
        assert await _visible_request_summaries() == {"b-request"}


async def test_insert_with_foreign_tenant_id_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """WITH CHECK политики: подлог чужого tenant_id отвергает БД, не дисциплина."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(RequestCategory(tenant_id=tenant_b, key="stolen", name="Stolen"))
            await session.flush()

    category_a = await make_category(tenant_a)
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(
                ServiceRequest(
                    tenant_id=tenant_b, category_id=category_a.id, summary="stolen request"
                )
            )
            await session.flush()


async def test_service_cannot_use_foreign_category(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Чужая категория для сервиса неотличима от несуществующей (RLS)."""
    tenant_a, tenant_b = two_tenants
    category_b = await make_category(tenant_b)

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await create_request(
            ServiceRequestCreate(category_id=category_b.id, summary="cross-tenant")
        )
    assert error.value.code == ERR_REQUESTS_CATEGORY_NOT_FOUND


async def test_service_cannot_touch_foreign_request(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    request_in_each_tenant: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Чужая заявка недоступна и для чтения, и для смены статуса."""
    tenant_a, _ = two_tenants
    _, request_b_id = request_in_each_tenant

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await change_request_status(request_b_id, RequestStatus.ASSIGNED)
    assert error.value.code == ERR_REQUESTS_REQUEST_NOT_FOUND


async def test_platform_scope_cannot_read_module_tables(
    request_in_each_tenant: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Платформенная сессия (без контекста тенанта) не видит бизнес-таблиц:
    исключение platform_dispatch существует только для outbox_events."""
    async with platform_session_scope() as session:
        categories = await session.scalar(select(func.count()).select_from(RequestCategory))
        requests = await session.scalar(select(func.count()).select_from(ServiceRequest))
    assert categories == 0
    assert requests == 0

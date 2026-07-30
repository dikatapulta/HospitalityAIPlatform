"""Звено `TenantResolver` кабинета + цепочка резолверов (spec 0033 §3.3/§10,
ADR-008 §6).

Юниты зовут `resolve_tenant_from_staff_session` на сфабрикованном ASGI-scope
(приём `_staff_request` из test_staff_auth); интеграционные — через настоящий
`create_app`: staff-cookie ставит контекст тенанта на страницах кабинета, но
НЕ открывает `/api/v1/*`, а сервисный токен работает как раньше (регресс
первого звена цепочки).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.types import Scope

from hospitality.app import create_app
from hospitality.platform.auth import resolve_tenant_from_staff_session
from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    Tenant,
    TenantMembership,
)
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, login
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import TenantResolver, chain_resolvers, current_tenant_id_or_none
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user

HOTEL_SLUG = "demo-hotel"


def _scope(path: str, cookie_token: str | None) -> dict[str, Any]:
    headers = []
    if cookie_token is not None:
        headers.append((b"cookie", f"{STAFF_SESSION_COOKIE}={cookie_token}".encode()))
    return {"type": "http", "path": path, "headers": headers}


@pytest.fixture(autouse=True)
def _fake_rate_limit() -> Iterator[None]:
    """Свой MonkeyPatch, НЕ общая фикстура monkeypatch (см. staff_portal/tests/
    conftest.py: autouse + общий monkeypatch ломает порядок undo POSTGRES_DB)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        "hospitality.shared.ratelimit.create_redis_client", lambda: FakeRateLimitRedis()
    )
    yield
    patcher.undo()


@pytest.fixture
async def hotel(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=HOTEL_SLUG, name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        return tenant


async def _staff_token(tenant_id: uuid.UUID, *, role: StaffRole = StaffRole.STAFF) -> str:
    email = f"resolver-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=tenant_id, role=role)
    grant = await login(email, PASSWORD, client_ip=f"ip-{uuid.uuid4().hex[:8]}")
    return grant.session_token


# ---------------------------------------------------------------------------
# Юниты звена (сфабрикованный scope)
# ---------------------------------------------------------------------------


async def test_valid_session_and_membership_resolve_tenant(hotel: Tenant) -> None:
    token = await _staff_token(hotel.id)
    resolved = await resolve_tenant_from_staff_session(_scope(f"/staff/{HOTEL_SLUG}", token))
    assert resolved == hotel.id
    # Вложенные пути кабинета резолвятся тем же звеном (страницы PR D/E/F).
    nested = await resolve_tenant_from_staff_session(_scope(f"/staff/{HOTEL_SLUG}/requests", token))
    assert nested == hotel.id


async def test_without_cookie_or_with_garbage_token_resolves_none(hotel: Tenant) -> None:
    assert await resolve_tenant_from_staff_session(_scope(f"/staff/{HOTEL_SLUG}", None)) is None
    assert (
        await resolve_tenant_from_staff_session(_scope(f"/staff/{HOTEL_SLUG}", "garbage")) is None
    )


async def test_unknown_slug_resolves_none(hotel: Tenant) -> None:
    token = await _staff_token(hotel.id)
    assert await resolve_tenant_from_staff_session(_scope("/staff/no-such-hotel", token)) is None


async def test_revoked_membership_closes_access_immediately(hotel: Tenant) -> None:
    token = await _staff_token(hotel.id)
    async with platform_session_scope() as session:
        membership = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.tenant_id == hotel.id)
            )
        ).one()
        membership.status = MembershipStatus.REVOKED
    assert await resolve_tenant_from_staff_session(_scope(f"/staff/{HOTEL_SLUG}", token)) is None


async def test_reserved_segments_and_foreign_paths_resolve_none(hotel: Tenant) -> None:
    token = await _staff_token(hotel.id)
    for path in ("/staff/login", "/staff/logout", "/staff/static/styles.css", "/staff/invite/x"):
        assert await resolve_tenant_from_staff_session(_scope(path, token)) is None
    assert await resolve_tenant_from_staff_session(_scope("/api/v1/requests", token)) is None
    assert await resolve_tenant_from_staff_session(_scope("/health/live", token)) is None


# ---------------------------------------------------------------------------
# Цепочка (юнит без БД)
# ---------------------------------------------------------------------------


async def test_chain_resolvers_first_match_wins() -> None:
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    calls: list[str] = []

    def _resolver(name: str, result: uuid.UUID | None) -> TenantResolver:
        async def resolve(scope: Scope) -> uuid.UUID | None:
            calls.append(name)
            return result

        return resolve

    chain = chain_resolvers(
        _resolver("a", None), _resolver("b", first_id), _resolver("c", second_id)
    )
    assert await chain(_scope("/any", None)) == first_id
    assert calls == ["a", "b"]  # третье звено не опрашивается — победило второе

    calls.clear()
    empty_chain = chain_resolvers(_resolver("a", None), _resolver("b", None))
    assert await empty_chain(_scope("/any", None)) is None
    assert calls == ["a", "b"]


# ---------------------------------------------------------------------------
# Интеграция через настоящее приложение
# ---------------------------------------------------------------------------


def _app_with_echo() -> FastAPI:
    app = create_app()

    @app.get("/staff/{tenant_slug}/echo-tenant")
    async def echo_tenant(tenant_slug: str) -> dict[str, str | None]:
        tenant_id = current_tenant_id_or_none()
        return {"tenant_id": str(tenant_id) if tenant_id else None}

    return app


async def test_staff_cookie_sets_tenant_context_on_portal_paths(hotel: Tenant) -> None:
    token = await _staff_token(hotel.id)
    async with AsyncClient(
        transport=ASGITransport(app=_app_with_echo()), base_url="https://test"
    ) as client:
        client.cookies.set(STAFF_SESSION_COOKIE, token)
        response = await client.get(f"/staff/{HOTEL_SLUG}/echo-tenant")
        assert response.status_code == 200
        assert response.json() == {"tenant_id": str(hotel.id)}


async def test_staff_cookie_does_not_open_service_api(hotel: Tenant) -> None:
    """Инвариант §11: staff-сессия — идентичность кабинета, а не сервисный токен."""
    token = await _staff_token(hotel.id)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        client.cookies.set(STAFF_SESSION_COOKIE, token)
        response = await client.get("/api/v1/requests")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "ERR-PLATFORM-007"


async def test_service_token_still_resolves_through_chain(
    hotel: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регресс первого звена: цепочка не сломала аутентификацию API-ключом."""
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")
    monkeypatch.setenv("SERVICE_TOKEN_TENANT_SLUG", HOTEL_SLUG)
    get_settings.cache_clear()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="https://test"
        ) as client:
            response = await client.get(
                "/api/v1/requests", headers={"Authorization": "Bearer test-service-token"}
            )
            assert response.status_code == 200
    finally:
        get_settings.cache_clear()

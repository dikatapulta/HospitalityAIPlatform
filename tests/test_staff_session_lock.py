"""Короткая сессия кабинета: «Запомнить меня» и запирание после простоя (spec 0073).

Без галочки сессия после `staff_session_short_idle_minutes` без действий человека
запирается: фоновое чтение (опрос сигнала) её получает с `locked=True` и не
продлевает, всё остальное — None, как истёкшую. Запомненная — прежние 30 дней.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from starlette.types import Scope

from hospitality.platform.auth import is_background_read
from hospitality.platform.models import StaffRole, StaffSession, Tenant
from hospitality.platform.staff_auth import (
    StaffSessionGrant,
    load_staff_context,
    login,
    resolve_staff_session,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login


@pytest.fixture(autouse=True)
def _fake_rate_limit() -> Iterator[None]:
    """Свой MonkeyPatch, не общая фикстура: autouse захватила бы её раньше
    `canonical_database` (канон `staff_portal/tests/conftest.py`)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        "hospitality.shared.ratelimit.create_redis_client", lambda: FakeRateLimitRedis()
    )
    yield
    patcher.undo()


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-lock", name="Hotel Lock")
        session.add(row)
        await session.flush()
        return row


async def _login(tenant: Tenant, *, remember: bool | None = None) -> StaffSessionGrant:
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    if remember is None:
        return await login(tenant.id, staff_login, PASSWORD, client_ip="ip-lock")
    return await login(tenant.id, staff_login, PASSWORD, client_ip="ip-lock", remember=remember)


async def _session_row() -> StaffSession:
    async with platform_session_scope() as session:
        return (await session.scalars(select(StaffSession))).one()


async def _set_last_used(seconds_ago: float) -> None:
    async with platform_session_scope() as session:
        row = (await session.scalars(select(StaffSession))).one()
        row.last_used_at = utc_now() - timedelta(seconds=seconds_ago)


def _short_idle_seconds() -> int:
    return get_settings().staff_session_short_idle_minutes * 60


async def test_login_stores_remember_choice_and_defaults_to_short(tenant: Tenant) -> None:
    await _login(tenant)
    assert (await _session_row()).remembered is False
    async with platform_session_scope() as session:
        for row in (await session.scalars(select(StaffSession))).all():
            await session.delete(row)
    await _login(tenant, remember=True)
    assert (await _session_row()).remembered is True


async def test_short_session_locks_after_idle_but_background_read_passes(
    tenant: Tenant,
) -> None:
    grant = await _login(tenant, remember=False)
    await _set_last_used(_short_idle_seconds() + 60)
    before = (await _session_row()).last_used_at

    assert await resolve_staff_session(grant.session_token) is None
    locked = await resolve_staff_session(grant.session_token, background=True)
    assert locked is not None and locked.locked is True
    # Запертую ничто не продлевает: ни опрос, ни отказанное действие.
    assert (await _session_row()).last_used_at == before
    assert await resolve_staff_session(grant.session_token) is None

    context = await load_staff_context(locked, tenant.slug)
    assert context is not None and context.session_locked is True


async def test_background_read_does_not_refresh_short_session(tenant: Tenant) -> None:
    grant = await _login(tenant, remember=False)
    await _set_last_used(5 * 60)
    before = (await _session_row()).last_used_at

    active = await resolve_staff_session(grant.session_token, background=True)
    assert active is not None and active.locked is False
    assert active.locks_at == before + timedelta(seconds=_short_idle_seconds())
    assert (await _session_row()).last_used_at == before

    # Действие человека продлевает.
    refreshed = await resolve_staff_session(grant.session_token)
    assert refreshed is not None and refreshed.locks_at is not None
    assert (await _session_row()).last_used_at > before
    assert refreshed.locks_at > before + timedelta(seconds=_short_idle_seconds())


async def test_short_session_is_refreshed_every_minute(tenant: Tenant) -> None:
    grant = await _login(tenant, remember=False)
    await _set_last_used(90)
    before = (await _session_row()).last_used_at
    assert await resolve_staff_session(grant.session_token) is not None
    assert (await _session_row()).last_used_at > before


async def test_remembered_session_keeps_old_rules(tenant: Tenant) -> None:
    """Запомненная: запись раз в 5 минут, опрос продлевает, запирания нет."""
    grant = await _login(tenant, remember=True)
    await _set_last_used(90)
    before = (await _session_row()).last_used_at
    active = await resolve_staff_session(grant.session_token)
    assert active is not None and active.locks_at is None and active.locked is False
    assert (await _session_row()).last_used_at == before

    await _set_last_used(60 * 60)
    before = (await _session_row()).last_used_at
    assert await resolve_staff_session(grant.session_token, background=True) is not None
    assert (await _session_row()).last_used_at > before


def _scope(method: str, sec_fetch_mode: str | None) -> Scope:
    headers = [(b"cookie", b"x=1")]
    if sec_fetch_mode is not None:
        headers.append((b"sec-fetch-mode", sec_fetch_mode.encode()))
    return {"type": "http", "method": method, "path": "/staff/h/x", "headers": headers}


@pytest.mark.parametrize(
    ("method", "mode", "background"),
    [
        ("GET", "cors", True),
        ("GET", "same-origin", True),
        ("HEAD", "no-cors", True),
        ("GET", "navigate", False),
        ("GET", "Navigate", False),
        ("GET", None, False),
        ("POST", "cors", False),
        ("POST", "navigate", False),
    ],
)
def test_background_read_rule(method: str, mode: str | None, background: bool) -> None:
    """Действие человека — переход, любой POST и запрос без заголовка (spec 0073 §2)."""
    assert is_background_read(_scope(method, mode)) is background

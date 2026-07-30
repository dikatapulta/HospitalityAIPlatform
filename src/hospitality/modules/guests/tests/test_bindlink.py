"""Одноразовая ссылка привязки (spec 0033 §6/§10, PR E серии #48).

Redis подменяется фейком с ручными часами (`FakeBindLinkRedis`): CI гоняет
юнит-тесты без живого Redis; TTL «наступает» advance'ом, не сном.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.guests.api import (
    BIND_LINK_TTL_SECONDS,
    ERR_GUESTS_BINDLINK_UNAVAILABLE,
    ERR_GUESTS_STAY_NOT_FOUND,
    GuestIdentityKind,
    GuestSessionBind,
    check_out,
    consume_bind_link,
    issue_bind_link,
    resolve_session,
    start_guest_session_for_stay,
)
from hospitality.modules.guests.models import GuestIdentity, GuestSession
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeBindLinkRedis


def _bind_data(stay_id: uuid.UUID) -> GuestSessionBind:
    return GuestSessionBind(
        stay_id=stay_id,
        identity_external_id=str(uuid.uuid4()),
        consent_version="v1",
    )


async def test_issue_and_consume_is_single_use(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Одноразовость держит GETDEL: второе потребление — отказ (§10)."""
    tenant_a, _ = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
        assert await consume_bind_link(token, client=fake) == result.stay.id
        assert await consume_bind_link(token, client=fake) is None


async def test_expired_link_is_rejected(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
        fake.advance(BIND_LINK_TTL_SECONDS + 1)
        assert await consume_bind_link(token, client=fake) is None


async def test_foreign_tenant_link_is_useless(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Ключ начинается с tenant_id (P-4): токен отеля A в контексте B мёртв."""
    tenant_a, tenant_b = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
    with tenant_context(tenant_b):
        assert await consume_bind_link(token, client=fake) is None
    # В своём контексте токен всё ещё жив (чужая попытка его не потратила).
    with tenant_context(tenant_a):
        assert await consume_bind_link(token, client=fake) == result.stay.id


async def test_bind_creates_session_via_the_same_path(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Привязка по ссылке рождает ту же пару идентичность+сессия, что ввод
    кода (P-12): kind=web, согласие на сессии, resolve_session работает."""
    tenant_a, _ = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
        stay_id = await consume_bind_link(token, client=fake)
        assert stay_id is not None

        grant = await start_guest_session_for_stay(_bind_data(stay_id))
        assert grant is not None
        assert grant.stay_id == result.stay.id
        assert grant.room_number == "101"

        active = await resolve_session(grant.session_token)
        assert active is not None
        assert active.stay_id == result.stay.id

        from sqlalchemy import select

        async with session_scope() as session:
            (identity,) = (await session.scalars(select(GuestIdentity))).all()
            (guest_session,) = (await session.scalars(select(GuestSession))).all()
    assert identity.kind is GuestIdentityKind.WEB
    assert guest_session.consent_version == "v1"


async def test_bind_rejected_when_stay_is_gone(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Stay погас между выпуском и потреблением — None, как при вводе кода."""
    tenant_a, _ = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
        await check_out(result.stay.id)
        stay_id = await consume_bind_link(token, client=fake)
        assert stay_id is not None  # токен жил своей жизнью в Redis…
        assert await start_guest_session_for_stay(_bind_data(stay_id)) is None


async def test_issue_requires_active_stay(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        with pytest.raises(AppError) as error:
            await issue_bind_link(uuid.uuid4(), client=FakeBindLinkRedis())
        assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND


async def test_redis_down_is_fail_closed(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Выпуск при упавшем Redis — явная ошибка персоналу (ERR-GUESTS-005);
    потребление — молчаливый отказ (гость всегда может ввести код)."""
    tenant_a, _ = two_tenants
    fake = FakeBindLinkRedis()
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id, client=fake)
        fake.fail = True
        with pytest.raises(AppError) as error:
            await issue_bind_link(result.stay.id, client=fake)
        assert error.value.code == ERR_GUESTS_BINDLINK_UNAVAILABLE
        assert await consume_bind_link(token, client=fake) is None

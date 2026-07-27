"""Привязка тройкой тенант+комната+код и сессии (spec 0027 §5: тесты 3, 6).

Отказ привязки — всегда None без уточнения причины: гостю не сообщается,
«почти угадал» ли он, перечисление занятых комнат по разнице ответов
невозможно.
"""

from __future__ import annotations

import uuid

from hospitality.modules.guests.api import (
    GuestSessionStart,
    resolve_session,
    start_guest_session,
)
from hospitality.modules.guests.models import GuestIdentityKind
from hospitality.modules.guests.service import normalize_access_code
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.tenancy import tenant_context


def _bind(code: str, room: str = "101", external_id: str | None = None) -> GuestSessionStart:
    return GuestSessionStart(
        room_number=room,
        code=code,
        identity_external_id=external_id or str(uuid.uuid4()),
        consent_version="v1",
    )


async def test_successful_binding_grants_session(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)

    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None
        assert grant.stay_id == result.stay.id
        assert grant.room_number == "101"

        active = await resolve_session(grant.session_token)
        assert active is not None
        assert active.stay_id == result.stay.id
        assert active.guest_identity_id == grant.guest_identity_id
        assert active.room_number == "101"


async def test_wrong_code_and_wrong_room_are_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Тройка (ADR-008 §3): код вне своей комнаты бесполезен."""
    tenant_a, _ = two_tenants
    result_101 = await check_in_room(tenant_a, "101")
    await check_in_room(tenant_a, "102")

    with tenant_context(tenant_a):
        # Неверный код в правильной комнате.
        assert await start_guest_session(_bind("XXXXXX", room="101")) is None
        # Правильный код 101 в комнате 102 (сосед сфотографировал чужой QR).
        assert await start_guest_session(_bind(result_101.access_code, room="102")) is None
        # Комната без активного Stay — тот же неразличимый отказ.
        assert await start_guest_session(_bind(result_101.access_code, room="999")) is None


async def test_code_input_is_normalized(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """`k7m 9qt` == `K7M-9QT` == `k7m9qt` (spec 0027 §1.2)."""
    code = "K7M-9QT"
    assert normalize_access_code("k7m 9qt") == "K7M9QT"
    assert normalize_access_code(code) == "K7M9QT"

    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    sloppy = f" {result.access_code[:3].lower()}-{result.access_code[3:].lower()} "
    with tenant_context(tenant_a):
        assert await start_guest_session(_bind(sloppy)) is not None


async def test_code_is_reusable_per_stay(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Семья/второе устройство (ADR-008 §3): каждая привязка — своя идентичность
    и своя живая сессия."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)

    with tenant_context(tenant_a):
        first = await start_guest_session(_bind(result.access_code))
        second = await start_guest_session(_bind(result.access_code))
        assert first is not None and second is not None
        assert first.guest_identity_id != second.guest_identity_id
        assert await resolve_session(first.session_token) is not None
        assert await resolve_session(second.session_token) is not None


async def test_rebinding_same_external_id_reuses_identity(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Повторная привязка того же устройства не плодит идентичности."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    device = str(uuid.uuid4())

    with tenant_context(tenant_a):
        first = await start_guest_session(_bind(result.access_code, external_id=device))
        second = await start_guest_session(_bind(result.access_code, external_id=device))
        assert first is not None and second is not None
        assert first.guest_identity_id == second.guest_identity_id
        assert first.session_token != second.session_token


async def test_garbage_session_token_resolves_to_none(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        assert await resolve_session("not-a-real-token") is None


async def test_binding_records_consent_and_web_kind(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Согласие фиксируется на сессии (юраудит 22.07); kind по умолчанию — web."""
    from sqlalchemy import select

    from hospitality.modules.guests.models import GuestIdentity, GuestSession
    from hospitality.shared.db import session_scope

    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None
        async with session_scope() as session:
            (identity,) = (await session.scalars(select(GuestIdentity))).all()
            (guest_session,) = (await session.scalars(select(GuestSession))).all()
    assert identity.kind is GuestIdentityKind.WEB
    assert guest_session.consent_version == "v1"
    assert guest_session.consent_at is not None

"""Операции кабинета над Stay (spec 0033 §6/§10, PR E серии #48):
guests_count, переселение, продление, счётчик привязок.

Ключевой инвариант ADR-008 §3: доступ гостя производен от Stay — переселение
и продление НЕ трогают код и сессии, они следуют сами.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from hospitality.modules.guests.api import (
    ERR_GUESTS_CHECK_OUT_IN_PAST,
    ERR_GUESTS_ROOM_OCCUPIED,
    GuestSessionStart,
    StayCheckIn,
    check_in,
    check_out,
    count_stay_sessions,
    extend_stay,
    find_active_stay,
    get_active_stay,
    move_stay,
    resolve_session,
    start_guest_session,
)
from hospitality.modules.guests.models import Stay
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


def _bind(code: str, room: str = "101") -> GuestSessionStart:
    return GuestSessionStart(
        room_number=room,
        code=code,
        identity_external_id=str(uuid.uuid4()),
        consent_version="v1",
    )


async def test_guests_count_is_stored_and_defaults_to_one(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Кнопки «1/2/3+» кабинета: «3+» хранится как 3; CLI без поля — 1."""
    tenant_a, _ = two_tenants
    default = await check_in_room(tenant_a)
    assert default.stay.guests_count == 1

    with tenant_context(tenant_a):
        result = await check_in(
            StayCheckIn(
                room_number="102",
                check_out_at=utc_now() + timedelta(days=1),
                guests_count=3,
            )
        )
        assert result.stay.guests_count == 3
        stored = await get_active_stay(result.stay.id)
    assert stored is not None
    assert stored.guests_count == 3


async def test_move_stay_keeps_code_and_sessions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Переселение: комната меняется, доступ следует сам (ADR-008 §3)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None

        moved = await move_stay(result.stay.id, "205")
        assert moved.room_number == "205"

        # Старая комната свободна, новая занята этим же Stay.
        assert await find_active_stay("101") is None
        relocated = await find_active_stay("205")
        assert relocated is not None and relocated.id == result.stay.id
        # Сессия жива и видит НОВУЮ комнату (комната — из Stay на каждом действии).
        active = await resolve_session(grant.session_token)
        assert active is not None
        assert active.room_number == "205"
        # Код продолжает работать — уже для новой комнаты.
        assert await start_guest_session(_bind(result.access_code, room="205")) is not None


async def test_move_to_occupied_room_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    first = await check_in_room(tenant_a, "101")
    await check_in_room(tenant_a, "102")

    with tenant_context(tenant_a):
        with pytest.raises(AppError) as error:
            await move_stay(first.stay.id, "102")
        assert error.value.code == ERR_GUESTS_ROOM_OCCUPIED
        # Комната не изменилась — транзакция откатилась.
        unchanged = await get_active_stay(first.stay.id)
    assert unchanged is not None
    assert unchanged.room_number == "101"


async def test_extend_stay_revives_expired_access(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Продление просроченного Stay оживляет сессии — валидность производна
    от Stay, отдельного действия с токенами нет (ADR-008 §3)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None

        async with session_scope() as session:
            stay = await session.get(Stay, result.stay.id)
            assert stay is not None
            stay.check_out_at = utc_now() - timedelta(minutes=1)
        assert await resolve_session(grant.session_token) is None

        new_check_out = utc_now() + timedelta(days=1)
        extended = await extend_stay(result.stay.id, new_check_out)
        assert extended.check_out_at == new_check_out
        assert await resolve_session(grant.session_token) is not None


async def test_extend_into_past_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        with pytest.raises(AppError) as error:
            await extend_stay(result.stay.id, utc_now() - timedelta(minutes=1))
        assert error.value.code == ERR_GUESTS_CHECK_OUT_IN_PAST


async def test_count_stay_sessions_tracks_bindings_and_checkout(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Счётчик привязок — индикатор «гость подключился» (spec 0033 §6):
    считает живые сессии, выезд обнуляет."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await count_stay_sessions(result.stay.id) == 0
        assert await start_guest_session(_bind(result.access_code)) is not None
        assert await start_guest_session(_bind(result.access_code)) is not None
        assert await count_stay_sessions(result.stay.id) == 2
        await check_out(result.stay.id)
        assert await count_stay_sessions(result.stay.id) == 0

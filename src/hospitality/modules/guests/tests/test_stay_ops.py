"""Операции кабинета над Stay (spec 0033 §6/§10, PR E серии #48):
guests_count, переселение и обмен комнатами (#237), продление, счётчик привязок.

Ключевой инвариант ADR-008 §3: доступ гостя производен от Stay — переселение
и продление НЕ трогают код и сессии, они следуют сами.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from hospitality.modules.guests.api import (
    ERR_GUESTS_CHECK_OUT_IN_PAST,
    ERR_GUESTS_ROOM_OCCUPIED,
    ERR_GUESTS_STAY_NOT_FOUND,
    MAX_GUESTS_PER_STAY,
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
    swap_stay_rooms,
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
    """Число гостей хранится как есть; без поля — 1; выше потолка — отказ схемы (#227)."""
    with pytest.raises(ValidationError):
        StayCheckIn(
            room_number="101",
            check_out_at=utc_now() + timedelta(days=1),
            guests_count=MAX_GUESTS_PER_STAY + 1,
        )
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


def test_room_number_with_slash_is_rejected() -> None:
    """«/» в номере — отказ схемы: адрес чата такой комнаты ведёт на 404 (#359).

    Прочие знаки свободного ввода (пробел, буквы, дефис) схема пропускает.
    """
    check_out_at = utc_now() + timedelta(days=1)
    for room in ("2/1", "/", "A/12"):
        with pytest.raises(ValidationError, match="pattern"):
            StayCheckIn(room_number=room, check_out_at=check_out_at)
    for room in ("305", "2 A", "Люкс-1"):
        assert StayCheckIn(room_number=room, check_out_at=check_out_at).room_number == room


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


async def test_swap_stay_rooms_keeps_both_guests_access(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Обмен комнатами (#237, аудит 05.08 Г-6): partial unique не мешает, коды
    и сессии обоих гостей живут и видят новые комнаты (ADR-008 §3)."""
    tenant_a, _ = two_tenants
    first = await check_in_room(tenant_a, "101")
    second = await check_in_room(tenant_a, "102")
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(first.access_code, room="101"))
        assert grant is not None

        swapped = await swap_stay_rooms(first.stay.id, "102")
        assert swapped.room_number == "102"

        in_101 = await find_active_stay("101")
        in_102 = await find_active_stay("102")
        assert in_101 is not None and in_101.id == second.stay.id
        assert in_102 is not None and in_102.id == first.stay.id
        active = await resolve_session(grant.session_token)
        assert active is not None and active.room_number == "102"
        assert await start_guest_session(_bind(second.access_code, room="101")) is not None
        # Своя же комната — без изменений.
        same = await swap_stay_rooms(first.stay.id, "102")
        assert same.room_number == "102"


async def test_swap_with_free_room_changes_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Гость той комнаты выехал, пока сотрудник подтверждал, — ERR-GUESTS-001, а не
    тихое переселение; комнаты не меняются. Чужой тенант — та же пустая комната (RLS)."""
    tenant_a, tenant_b = two_tenants
    first = await check_in_room(tenant_a, "101")
    second = await check_in_room(tenant_a, "102")
    await check_in_room(tenant_b, "103")
    with tenant_context(tenant_a):
        await check_out(second.stay.id)
        for room in ("102", "103"):
            with pytest.raises(AppError) as error:
                await swap_stay_rooms(first.stay.id, room)
            assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND
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

"""Жизненный цикл Stay и кода заселения (spec 0027 §1.2, §5: тесты 2, 4, 5).

Ключевой инвариант DoD #79 — «истёкшая сессия не может действовать»:
выезд и наступление `check_out_at` гасят доступ, `resolve_session` даёт None.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.guests.api import (
    ERR_GUESTS_ROOM_OCCUPIED,
    ERR_GUESTS_STAY_NOT_FOUND,
    GuestSessionStart,
    StayStatus,
    check_out,
    find_active_stay,
    format_access_code,
    list_active_stays,
    reissue_access_code,
    resolve_session,
    start_guest_session,
)
from hospitality.modules.guests.models import Stay, StayAccessCode
from hospitality.modules.guests.service import ACCESS_CODE_ALPHABET, ACCESS_CODE_LENGTH
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context


def _bind(code: str, room: str = "101") -> GuestSessionStart:
    return GuestSessionStart(
        room_number=room,
        code=code,
        identity_external_id=str(uuid.uuid4()),
        consent_version="v1",
    )


async def _outbox_event_names() -> set[str]:
    async with platform_session_scope() as session:
        rows = await session.scalars(select(OutboxEvent.event_name))
        return set(rows.all())


async def test_check_in_issues_code_once_and_stores_only_hash(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)

    assert result.stay.status is StayStatus.CHECKED_IN
    assert len(result.access_code) == ACCESS_CODE_LENGTH
    assert set(result.access_code) <= set(ACCESS_CODE_ALPHABET)
    assert format_access_code(result.access_code) == (
        f"{result.access_code[:3]}-{result.access_code[3:]}"
    )
    # В БД — только bcrypt-хэш, plaintext не хранится нигде (ADR-008).
    with tenant_context(tenant_a):
        async with session_scope() as session:
            (stored,) = (await session.scalars(select(StayAccessCode))).all()
    assert stored.code_hash.startswith("$2")
    assert result.access_code not in stored.code_hash
    assert "stay.checked_in" in await _outbox_event_names()


async def test_second_check_in_of_same_room_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Один активный Stay на комнату — свойство БД (partial unique)."""
    tenant_a, _ = two_tenants
    await check_in_room(tenant_a)

    with pytest.raises(AppError) as error:
        await check_in_room(tenant_a)
    assert error.value.code == ERR_GUESTS_ROOM_OCCUPIED


async def test_reissue_kills_old_code_but_not_sessions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """ADR-008 §3: новый код гасит старый; уже созданные сессии живут."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None

        new_code = await reissue_access_code(result.stay.id)

        # Старый код мёртв, новый жив.
        assert await start_guest_session(_bind(result.access_code)) is None
        assert await start_guest_session(_bind(new_code)) is not None
        # Существующая сессия продолжает действовать.
        assert await resolve_session(grant.session_token) is not None


async def test_check_out_revokes_code_and_sessions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Q8: после выезда доступ гаснет целиком, grace-периода нет."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None

        closed = await check_out(result.stay.id)

        assert closed.status is StayStatus.CHECKED_OUT
        assert await resolve_session(grant.session_token) is None
        assert await start_guest_session(_bind(result.access_code)) is None
        assert await find_active_stay("101") is None
        # Повторный выезд — уже не активный Stay.
        with pytest.raises(AppError) as error:
            await check_out(result.stay.id)
        assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND
    assert "stay.checked_out" in await _outbox_event_names()


async def test_expired_stay_cannot_act(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Инвариант issue #79: наступление check_out_at (без явного check_out)
    гасит и сессию, и привязку по коду — валидность производна от Stay."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_bind(result.access_code))
        assert grant is not None

        # Время «наступает»: сдвигаем check_out_at в прошлое прямо в БД.
        async with session_scope() as session:
            stay = await session.get(Stay, result.stay.id)
            assert stay is not None
            stay.check_out_at = utc_now() - timedelta(minutes=1)

        assert await resolve_session(grant.session_token) is None
        assert await start_guest_session(_bind(result.access_code)) is None
        assert await find_active_stay("101") is None


async def test_list_active_stays_orders_by_room(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await check_in_room(tenant_a, "202")
    await check_in_room(tenant_a, "101")
    with tenant_context(tenant_a):
        stays = await list_active_stays()
    assert [stay.room_number for stay in stays] == ["101", "202"]

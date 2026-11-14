"""Подключения гостей за окно — число «пульса пользы» (spec 0035 §8.1, #1050 ч. 2)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from hospitality.modules.guests.api import (
    GuestSessionStart,
    count_guest_logins,
    start_guest_session,
)
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context


async def _connect(tenant_id: uuid.UUID, room: str, times: int) -> None:
    result = await check_in_room(tenant_id, room)
    with tenant_context(tenant_id):
        for _ in range(times):
            grant = await start_guest_session(
                GuestSessionStart(
                    room_number=room,
                    code=result.access_code,
                    identity_external_id=str(uuid.uuid4()),
                    consent_version="v1",
                )
            )
            assert grant is not None


async def test_counts_new_sessions_of_the_hotel_in_the_window(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await _connect(tenant_a, "101", 2)
    await _connect(tenant_a, "102", 1)
    await _connect(tenant_b, "101", 4)
    now = utc_now()

    with tenant_context(tenant_a):
        assert (
            await count_guest_logins(created_after=now - timedelta(hours=1), created_before=now)
            == 3
        )
        # Окно после подключений — пусто: считается момент подключения, а не жизнь сессии.
        assert (
            await count_guest_logins(created_after=now, created_before=now + timedelta(hours=1))
            == 0
        )

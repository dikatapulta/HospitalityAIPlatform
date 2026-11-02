"""Поздний выезд в модуле guests (spec 0060 §2.1, §3, §4 п. 1–3, 8).

Поздний выезд — тот же `check_out_at`, только позже расчётного часа: доступ гостя,
привязка по QR и автовыезд следуют за сроком сами. «Сейчас» модуля сдвигается
подменой `utc_now` — так «после 16:00» проверяется без ожидания.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from hospitality.modules.guests import bindlink, expiry, service
from hospitality.modules.guests.api import (
    ERR_GUESTS_CHECK_OUT_IN_PAST,
    ERR_GUESTS_STAY_NOT_FOUND,
    GuestSessionBind,
    GuestSessionStart,
    check_out,
    close_overdue_stays,
    get_active_stay,
    issue_bind_link,
    list_stays_leaving_between,
    resolve_session,
    set_checkout_time,
    start_guest_session,
    start_guest_session_by_bind_link,
)
from hospitality.modules.guests.models import Stay
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def _due(tenant_id: uuid.UUID, stay_id: uuid.UUID, moment: datetime) -> None:
    """Срок Stay — `moment` в обход кабинета: «расчётный час уже был»."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            stay = await session.get(Stay, stay_id)
            assert stay is not None
            stay.check_out_at = moment


def _shift_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """«Сейчас» сессии гостя, привязки по QR и автовыезда — `moment`."""
    for module in (service, bindlink, expiry):
        monkeypatch.setattr(module, "utc_now", lambda: moment)


async def test_set_checkout_time_moves_deadline_and_rejects_past(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Новый срок — как есть; прошлое — ERR-GUESTS-004, срок не тронут. Строку
    журнала «Время выезда» проверяет кабинет: ей нужен настоящий сотрудник."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    late = result.stay.check_out_at + timedelta(hours=4)
    with tenant_context(tenant_a):
        updated = await set_checkout_time(result.stay.id, late)
        assert updated.check_out_at == late
        with pytest.raises(AppError) as error:
            await set_checkout_time(result.stay.id, utc_now() - timedelta(minutes=1))
        assert error.value.code == ERR_GUESTS_CHECK_OUT_IN_PAST
        stored = await get_active_stay(result.stay.id)
    assert stored is not None and stored.check_out_at == late


async def test_set_checkout_time_of_closed_stay_is_not_found(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        await check_out(result.stay.id)
        with pytest.raises(AppError) as error:
            await set_checkout_time(result.stay.id, utc_now() + timedelta(hours=2))
    assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND


async def test_late_checkout_keeps_guest_in_until_new_deadline(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """«13:00»: расчётный час прошёл час назад, поздний выезд до «16:00» — сессия,
    код и QR живы; просроченный внутри запаса Stay после отметки снова пускает
    гостя. После «16:00» — ни сессии, ни привязки (§4 п. 2)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    now = utc_now()
    with tenant_context(tenant_a):
        grant = await start_guest_session(
            GuestSessionStart(
                room_number="101",
                code=result.access_code,
                identity_external_id=str(uuid.uuid4()),
                consent_version="v1",
            )
        )
        assert grant is not None
        link = await issue_bind_link(result.stay.id)
    await _due(tenant_a, result.stay.id, now - timedelta(hours=1))
    with tenant_context(tenant_a):
        assert await resolve_session(grant.session_token) is None

        await set_checkout_time(result.stay.id, now + timedelta(hours=3))
        assert await resolve_session(grant.session_token) is not None
        assert await start_guest_session_by_bind_link(_bind(link)) is not None

        _shift_clock(monkeypatch, now + timedelta(hours=3, minutes=1))
        assert await resolve_session(grant.session_token) is None
        assert await start_guest_session_by_bind_link(_bind(link)) is None


async def test_auto_checkout_waits_for_late_checkout_plus_grace(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Расчётный час был 3 ч назад (запас 2 ч вышел), поздний выезд — через час:
    автовыезд не трогает Stay до срока + 2 ч и закрывает после (§4 п. 3)."""
    tenant_a, _ = two_tenants
    await _configure(tenant_a)
    result = await check_in_room(tenant_a)
    now = utc_now()
    await _due(tenant_a, result.stay.id, now - timedelta(hours=3))
    late = now + timedelta(hours=1)
    with tenant_context(tenant_a):
        await set_checkout_time(result.stay.id, late)

    assert await close_overdue_stays() == 0
    _shift_clock(monkeypatch, late + timedelta(hours=1, minutes=59))
    assert await close_overdue_stays() == 0
    _shift_clock(monkeypatch, late + timedelta(hours=2, minutes=1))
    assert await close_overdue_stays() == 1
    with tenant_context(tenant_a):
        assert await get_active_stay(result.stay.id) is None


async def test_list_stays_leaving_between_is_strict_ordered_and_isolated(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Строго между границами, по сроку, затем по номеру; чужой тенант не виден (§4 п. 8)."""
    tenant_a, tenant_b = two_tenants
    noon = utc_now().replace(microsecond=0) + timedelta(days=1)
    midnight = noon + timedelta(hours=12)
    for room, moment in (
        ("704", noon + timedelta(hours=4)),
        ("305", noon + timedelta(hours=2)),
        ("203", noon + timedelta(hours=4)),
        ("101", noon),
        ("909", midnight),
    ):
        stay = await check_in_room(tenant_a, room)
        await _due(tenant_a, stay.stay.id, moment)
    other = await check_in_room(tenant_b, "505")
    await _due(tenant_b, other.stay.id, noon + timedelta(hours=3))

    with tenant_context(tenant_a):
        leaving = await list_stays_leaving_between(noon, midnight)
    assert [stay.room_number for stay in leaving] == ["305", "203", "704"]


async def _configure(tenant_id: uuid.UUID) -> None:
    """Конфиг с автовыездом через 2 ч после срока (умолчание spec 0043 §2.1)."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                stay_auto_checkout_grace_hours=2,
            ),
        )


def _bind(token: str) -> GuestSessionBind:
    return GuestSessionBind(
        bind_token=token, identity_external_id=str(uuid.uuid4()), consent_version="v1"
    )

"""Автовыезд по сроку и выборка просроченных (spec 0043, issue #225).

Инварианты: Stay закрывается только за запасом тенанта и тем же путём, что
ручной выезд (код, ссылки, сессии гаснут, `stay.checked_out` в outbox), с
журналом «автоматически»; чужой тенант, тенант без конфига и с выключенным
автовыездом не тронуты; строку, которую держит сотрудник, джоба пропускает.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.guests.api import (
    ERR_GUESTS_STAY_NOT_FOUND,
    GuestSessionStart,
    close_overdue_stays,
    extend_stay,
    get_active_stay,
    issue_bind_link,
    list_overdue_stays,
    resolve_session,
    start_guest_session,
)
from hospitality.modules.guests.models import Stay, StayAccessCode, StayBindLink, StayStatus
from hospitality.modules.guests.service import close_stay
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffAction, StaffActionLog
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context
from tests.test_tenant_config import _wait_until_a_backend_blocks


async def _configure(tenant_id: uuid.UUID, *, grace_hours: int | None = 2) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                stay_auto_checkout_grace_hours=grace_hours,
            ),
        )


async def _overdue_by(tenant_id: uuid.UUID, stay_id: uuid.UUID, delta: timedelta) -> None:
    """Сдвинуть срок выезда в прошлое на `delta` — «расчётный час был delta назад»."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            stay = await session.get(Stay, stay_id)
            assert stay is not None
            stay.check_out_at = utc_now() - delta


async def _status(tenant_id: uuid.UUID, stay_id: uuid.UUID) -> StayStatus:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            stay = await session.get(Stay, stay_id)
            assert stay is not None
            return stay.status


async def test_stay_past_grace_is_closed_like_manual_checkout(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await _configure(tenant_a)
    result = await check_in_room(tenant_a, "101")
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
        await issue_bind_link(result.stay.id)
    await _overdue_by(tenant_a, result.stay.id, timedelta(hours=3))

    assert await close_overdue_stays() == 1

    with tenant_context(tenant_a):
        assert await get_active_stay(result.stay.id) is None
        assert await resolve_session(grant.session_token) is None
        async with session_scope() as session:
            codes = list(await session.scalars(select(StayAccessCode)))
            links = list(await session.scalars(select(StayBindLink)))
            journal = list(await session.scalars(select(StaffActionLog)))
        assert all(code.revoked_at is not None for code in codes)
        assert links and all(link.revoked_at is not None for link in links)
        assert [
            (row.action, row.room_number, row.actor_user_id, row.actor_login) for row in journal
        ] == [(StaffAction.STAY_AUTO_CHECKED_OUT, "101", None, None)]
    async with platform_session_scope() as session:
        assert "stay.checked_out" in set(await session.scalars(select(OutboxEvent.event_name)))
    # Повторный прогон — ничего: Stay уже закрыт.
    assert await close_overdue_stays() == 0


async def test_only_stays_past_grace_of_configured_tenants_are_closed(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Внутри запаса — продлевается, а не закрывается; тенант без конфига
    (`tenant_b`) джоба не обходит вовсе — его Stay не тронут, чей бы ни был срок."""
    tenant_a, tenant_b = two_tenants
    await _configure(tenant_a, grace_hours=2)
    past = await check_in_room(tenant_a, "101")
    within = await check_in_room(tenant_a, "102")
    future = await check_in_room(tenant_a, "103")
    foreign = await check_in_room(tenant_b, "101")
    await _overdue_by(tenant_a, past.stay.id, timedelta(hours=2, minutes=1))
    await _overdue_by(tenant_a, within.stay.id, timedelta(hours=1, minutes=59))
    await _overdue_by(tenant_b, foreign.stay.id, timedelta(days=3))

    assert await close_overdue_stays() == 1

    assert await _status(tenant_a, past.stay.id) is StayStatus.CHECKED_OUT
    assert await _status(tenant_a, within.stay.id) is StayStatus.CHECKED_IN
    assert await _status(tenant_a, future.stay.id) is StayStatus.CHECKED_IN
    assert await _status(tenant_b, foreign.stay.id) is StayStatus.CHECKED_IN


async def test_disabled_auto_checkout_closes_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await _configure(tenant_a, grace_hours=None)
    result = await check_in_room(tenant_a, "101")
    await _overdue_by(tenant_a, result.stay.id, timedelta(days=3))

    assert await close_overdue_stays() == 0
    assert await _status(tenant_a, result.stay.id) is StayStatus.CHECKED_IN


async def test_stay_locked_by_staff_is_skipped(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сотрудник держит строку (`FOR UPDATE` действия над Stay) — джоба не ждёт
    и не трогает её (`SKIP LOCKED`); следующий прогон её подберёт."""
    tenant_a, _ = two_tenants
    await _configure(tenant_a)
    result = await check_in_room(tenant_a, "101")
    await _overdue_by(tenant_a, result.stay.id, timedelta(hours=3))

    with tenant_context(tenant_a):
        async with session_scope() as session:
            await session.scalar(select(Stay).where(Stay.id == result.stay.id).with_for_update())
            assert await close_overdue_stays() == 0
    assert await close_overdue_stays() == 1


async def test_extend_after_auto_checkout_is_not_found(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """После автовыезда «Продлить» честно отвечает «проживание закрыто», а не
    «продлено» у закрытого Stay (spec 0043 §2.2)."""
    tenant_a, _ = two_tenants
    await _configure(tenant_a)
    result = await check_in_room(tenant_a, "101")
    await _overdue_by(tenant_a, result.stay.id, timedelta(hours=3))
    await close_overdue_stays()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await extend_stay(result.stay.id, utc_now() + timedelta(days=1))
    assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND


async def test_list_overdue_stays_oldest_first_without_grace(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Просрочен — с `check_out_at`, а не с момента автовыезда: Stay внутри
    запаса в списке есть, будущий — нет."""
    tenant_a, _ = two_tenants
    recent = await check_in_room(tenant_a, "201")
    oldest = await check_in_room(tenant_a, "305")
    await check_in_room(tenant_a, "102")
    await _overdue_by(tenant_a, recent.stay.id, timedelta(minutes=5))
    await _overdue_by(tenant_a, oldest.stay.id, timedelta(days=1))

    with tenant_context(tenant_a):
        overdue = await list_overdue_stays()
    assert [stay.room_number for stay in overdue] == ["305", "201"]


async def test_extend_racing_auto_checkout_reports_closed_stay(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Продлить» в секунду автовыезда: джоба уже держит строку, продление встало
    на блокировку. После коммита джобы продление обязано ответить «проживание
    закрыто» (ERR-GUESTS-001), а не записать срок закрытому Stay и сказать
    сотруднику «продлено» (spec 0043 §2.2). Чередование задано явно — канон
    `test_mutate_tenant_config_does_not_lose_a_concurrent_edit`."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a, "101")
    await _overdue_by(tenant_a, result.stay.id, timedelta(hours=3))

    with tenant_context(tenant_a):
        async with session_scope() as session:
            stay = await session.scalar(
                select(Stay).where(Stay.id == result.stay.id).with_for_update()
            )
            assert stay is not None
            extend = asyncio.create_task(extend_stay(result.stay.id, utc_now() + timedelta(days=1)))
            assert await _wait_until_a_backend_blocks()
            await close_stay(session, stay, utc_now())
        with pytest.raises(AppError) as error:
            await extend
    assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND
    assert await _status(tenant_a, result.stay.id) is StayStatus.CHECKED_OUT

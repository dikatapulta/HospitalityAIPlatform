"""Журнал изменений кабинета — ядро (spec 0041 §6 пп. 2–4).

Изоляция тенантов на таблице `staff_action_log` (P-4, образец —
`tests/test_tenant_isolation.py`), пути без личности строк не пишут, текстов
гостя в журнале нет. Действия с личностью через HTTP кабинета, страница и
фильтры — `staff_portal/tests/test_changes.py`; ретеншн — в тестах
`channels/common`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole, Tenant
from hospitality.platform.staff_actions import (
    StaffActionRecord,
    StaffActor,
    list_staff_action_logins,
    record_staff_action,
)
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from tests.test_staff_auth import create_staff_user, unique_login

# Маркеры латиницей: кириллица в JSON экранируется, и проверка «маркера нет»
# прошла бы на экранированной строке, даже если текст утёк.
SUMMARY_MARKER = "MARKERSUMMARYQX"
DETAILS_MARKER = "MARKERDETAILSQX"
NOTE_MARKER = "MARKERNOTEQX"
GUEST_MARKER = "MARKERGUESTQX"
STAFF_MARKER = "MARKERSTAFFQX"


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def _actor(tenant_id: uuid.UUID, *, display_name: str = "Test Staff") -> StaffActor:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=tenant_id, role=StaffRole.MANAGER, display_name=display_name
    )
    return StaffActor(user_id=user_id, display_name=display_name, login=staff_login)


async def _category(tenant_id: uuid.UUID) -> requests_api.RequestCategoryRead:
    with tenant_context(tenant_id):
        return await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )


async def _journal(tenant_id: uuid.UUID) -> list[StaffActionLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(StaffActionLog)))


async def test_tenant_sees_only_own_journal_rows(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """P-4: строки журнала A не видны в контексте B и наоборот, в том числе
    сырым SQL без фильтра по tenant_id, и логины соседа не попадают в фильтр."""
    tenant_a, tenant_b = two_tenants
    actor_a = await _actor(tenant_a)
    actor_b = await _actor(tenant_b)
    for tenant_id, actor, room in ((tenant_a, actor_a, "101"), (tenant_b, actor_b, "202")):
        with tenant_context(tenant_id):
            async with session_scope() as session:
                record_staff_action(
                    session,
                    actor,
                    StaffActionRecord(action=StaffAction.STAY_CHECKED_IN, room_number=room),
                )

    assert [row.room_number for row in await _journal(tenant_a)] == ["101"]
    assert [row.room_number for row in await _journal(tenant_b)] == ["202"]
    with tenant_context(tenant_a):
        async with session_scope() as session:
            raw = (await session.execute(text("SELECT room_number FROM staff_action_log"))).all()
        assert [row[0] for row in raw] == ["101"]
        assert await list_staff_action_logins() == [actor_a.login]


async def test_insert_with_foreign_tenant_id_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(StaffActionLog(tenant_id=tenant_b, action=StaffAction.STAY_CHECKED_IN))
            await session.flush()

    assert await _journal(tenant_b) == []


async def test_paths_without_identity_write_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.2: заявка от ИИ, переходы из Telegram-пути, отмена гостем и
    заселение из CLI идут без личности — журнал молчит."""
    tenant_id, _ = two_tenants
    category = await _category(tenant_id)
    with tenant_context(tenant_id):
        by_ai = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="ai request",
            )
        )
        await requests_api.change_request_status(
            by_ai.id,
            requests_api.RequestStatus.IN_PROGRESS,
            initiator=requests_api.RequestInitiator.STAFF,
        )
        await requests_api.change_request_status(
            by_ai.id,
            requests_api.RequestStatus.DONE,
            initiator=requests_api.RequestInitiator.STAFF,
        )
        by_guest = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="guest changed mind",
            )
        )
        await requests_api.change_request_status(
            by_guest.id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
        )
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="101", check_out_at=utc_now() + timedelta(days=1))
        )
        await guests_api.check_out(checked.stay.id)

    assert await _journal(tenant_id) == []


async def test_journal_holds_no_guest_text_or_names(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.1: ни суть, ни детали, ни примечание заявки, ни имя гостя, ни имя
    сотрудника в журнал не попадают — ни в одну колонку."""
    tenant_id, _ = two_tenants
    actor = await _actor(tenant_id, display_name=STAFF_MARKER)
    category = await _category(tenant_id)
    with tenant_context(tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
                summary=SUMMARY_MARKER,
                details=DETAILS_MARKER,
                room_number="305",
            ),
            acting_user=actor,
        )
        await requests_api.change_request_status(
            created.id, requests_api.RequestStatus.IN_PROGRESS, acting_user=actor
        )
        await requests_api.change_request_status(
            created.id,
            requests_api.RequestStatus.DONE,
            resolution_note=NOTE_MARKER,
            acting_user=actor,
        )
        await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number="101",
                check_out_at=utc_now() + timedelta(days=1),
                guest_display_name=GUEST_MARKER,
            ),
            acting_user=actor,
        )
        async with session_scope() as session:
            dump = [
                row[0]
                for row in await session.execute(
                    text("SELECT row_to_json(log)::text FROM staff_action_log AS log")
                )
            ]

    assert len(dump) == 4
    for marker in (SUMMARY_MARKER, DETAILS_MARKER, NOTE_MARKER, GUEST_MARKER, STAFF_MARKER):
        assert not any(marker in row for row in dump), marker
    # Мутация-контроль: сам приём видит то, что в журнале действительно есть.
    assert all(actor.login is not None and actor.login in row for row in dump)

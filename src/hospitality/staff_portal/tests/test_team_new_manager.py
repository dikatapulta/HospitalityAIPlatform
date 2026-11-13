"""Новый менеджер отеля не появляется молча (#970, находки В1-5 и В2-1 аудита 10.11).

Приглашение уровня «Менеджер» — по одному, списком, «Новой ссылкой» — и подъём уровня
до него пишут строку журнала изменений и уведомление основателю; ожидающих приглашений
этого уровня — не больше потолка; отключение выпустившего гасит его приглашения уровней
выше «Сотрудника».
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_invites
from hospitality.platform.events import SuperadminActionKind
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole
from hospitality.platform.staff_invites import ERR_AUTH_MANAGER_INVITES_PENDING, create_invite
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
TEAM_API = f"/staff/{HOTEL_SLUG}/api/team"


async def _post(
    client: AsyncClient, path: str, payload: dict[str, object] | None = None
) -> httpx.Response:
    return await client.post(f"{TEAM_API}{path}", json=payload or {}, headers=SAME_ORIGIN)


def _services(role: str) -> list[str]:
    # «Сотруднику» и «Старшему» нужна служба (spec 0045 §6), менеджеру — нет.
    return [] if role == "manager" else ["reception"]


async def _invite(client: AsyncClient, role: str) -> httpx.Response:
    return await _post(
        client,
        "/invites",
        {
            "invited_name": "Новый",
            "login": unique_login(),
            "role_key": role,
            "service_keys": _services(role),
        },
    )


async def _batch(client: AsyncClient, role: str, size: int) -> httpx.Response:
    rows = [{"invited_name": f"Новый {n}", "login": unique_login()} for n in range(size)]
    return await _post(
        client,
        "/invites/batch",
        {"role_key": role, "service_keys": _services(role), "rows": rows},
    )


@pytest.fixture(autouse=True)
async def _reception(portal_hotel: PortalHotel) -> None:
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )


async def _journal(tenant_id: uuid.UUID) -> list[tuple[StaffAction, str | None]]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(StaffActionLog).order_by(StaffActionLog.occurred_at, StaffActionLog.id)
            )
            return [(row.action, row.actor_login) for row in rows]


async def _alerts() -> list[dict[str, object]]:
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.event_name == "staff.superadmin_acted")
            .order_by(OutboxEvent.occurred_at)
        )
        return [row.payload for row in rows]


async def test_manager_invite_by_every_door_leaves_journal_row_and_alert(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)

    single = await _invite(client, "manager")
    assert single.status_code == 200
    assert (await _batch(client, "manager", 2)).status_code == 200
    invite_id = (await staff_invites.list_pending_invites(portal_hotel.tenant_id))[0].invite_id
    assert (await _post(client, f"/invites/{invite_id}/reissue")).status_code == 200
    # Остальные уровни — без следа: их сотни в день запуска.
    assert (await _invite(client, "senior")).status_code == 200
    assert (await _batch(client, "staff", 3)).status_code == 200

    issued = (StaffAction.MANAGER_INVITE_ISSUED, portal_hotel.login)
    assert await _journal(portal_hotel.tenant_id) == [issued] * 4
    alerts = await _alerts()
    assert [(alert["kind"], alert["count"]) for alert in alerts] == [
        (SuperadminActionKind.MANAGER_INVITE_ISSUED.value, 1),
        (SuperadminActionKind.MANAGER_INVITE_ISSUED.value, 2),
        (SuperadminActionKind.MANAGER_INVITE_ISSUED.value, 1),
    ]
    assert {alert["actor_user_id"] for alert in alerts} == {str(portal_hotel.user_id)}
    page = await client.get(f"/staff/{HOTEL_SLUG}/changes")
    assert page.text.count("Приглашение менеджера") >= 4


async def test_raising_to_manager_leaves_journal_row_and_alert(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    member_id = await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.SENIOR,
        service_keys=("reception",),
    )
    await submit_login(client, portal_hotel.login)

    raised = await _post(client, f"/members/{member_id}/access", {"role_key": "manager"})
    assert raised.status_code == 200
    # Менеджер остаётся менеджером — не новый менеджер, следа нет.
    again = await _post(client, f"/members/{member_id}/access", {"role_key": "manager"})
    assert again.status_code == 200

    assert await _journal(portal_hotel.tenant_id) == [
        (StaffAction.MEMBER_MADE_MANAGER, portal_hotel.login)
    ]
    [alert] = await _alerts()
    assert alert["kind"] == SuperadminActionKind.MEMBER_MADE_MANAGER.value
    assert alert["target_user_id"] == str(member_id)
    assert alert["target_role"] == StaffRole.SENIOR.value
    assert "Назначен менеджер" in (await client.get(f"/staff/{HOTEL_SLUG}/changes")).text


async def test_deactivated_inviter_manager_invite_is_not_accepted(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Менеджер выпустил себе приглашение «Менеджер» перед отключением — после отключения
    ссылка мертва (410); приглашение «Сотрудник» того же менеджера живо: отключение
    не гасит сотни законных карточек."""
    tenant_id, inviter = portal_hotel.tenant_id, portal_hotel.user_id
    manager_invite = await create_invite(
        tenant_id, StaffRole.MANAGER, "Себе", unique_login(), invited_by=inviter
    )
    senior_invite = await create_invite(
        tenant_id, StaffRole.SENIOR, "Старший", unique_login(), invited_by=inviter
    )
    staff_invite = await create_invite(
        tenant_id, StaffRole.STAFF, "Горничная", unique_login(), invited_by=inviter
    )
    other_login = unique_login()
    await create_staff_user(other_login, tenant_id=tenant_id, role=StaffRole.MANAGER)
    await submit_login(client, other_login)

    assert (await _post(client, f"/members/{inviter}/deactivate")).status_code == 200

    for grant in (manager_invite, senior_invite):
        dead = await second_client.post(
            f"/staff/invite/{grant.invite_token}", data={"password": PASSWORD}
        )
        assert dead.status_code == 410
    alive = await second_client.post(
        f"/staff/invite/{staff_invite.invite_token}", data={"password": PASSWORD}
    )
    assert alive.status_code == 303
    assert await staff_invites.list_pending_invites(tenant_id) == []


async def test_manager_lowered_to_senior_loses_issued_manager_invites(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Хвост #970 (ревью PR #1115): снятие с «Менеджера» гасит его непринятые
    приглашения «Менеджер» и «Старший», как отключение; «Сотрудник» живо."""
    tenant_id = portal_hotel.tenant_id
    lowered_login = unique_login()
    lowered = await create_staff_user(lowered_login, tenant_id=tenant_id, role=StaffRole.MANAGER)
    manager_invite = await create_invite(
        tenant_id, StaffRole.MANAGER, "Себе", unique_login(), invited_by=lowered
    )
    staff_invite = await create_invite(
        tenant_id, StaffRole.STAFF, "Горничная", unique_login(), invited_by=lowered
    )
    await submit_login(client, portal_hotel.login)

    lowered_access: dict[str, object] = {"role_key": "senior", "service_keys": _services("senior")}
    response = await _post(client, f"/members/{lowered}/access", lowered_access)
    assert response.status_code == 200

    dead = await second_client.post(
        f"/staff/invite/{manager_invite.invite_token}", data={"password": PASSWORD}
    )
    assert dead.status_code == 410
    alive = await second_client.post(
        f"/staff/invite/{staff_invite.invite_token}", data={"password": PASSWORD}
    )
    assert alive.status_code == 303


async def test_pending_manager_invites_are_capped(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staff_invites, "MAX_PENDING_MANAGER_INVITES", 2)
    await submit_login(client, portal_hotel.login)
    assert (await _batch(client, "manager", 2)).status_code == 200

    for refused in (await _invite(client, "manager"), await _batch(client, "manager", 1)):
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == ERR_AUTH_MANAGER_INVITES_PENDING
    # Другие уровни потолок не держит.
    assert (await _batch(client, "staff", 3)).status_code == 200
    # Отозвал лишнее — место освободилось.
    pending = await staff_invites.list_pending_invites(portal_hotel.tenant_id)
    manager_id = next(i.invite_id for i in pending if i.role_key is StaffRole.MANAGER)
    assert (await _post(client, f"/invites/{manager_id}/revoke")).status_code == 200
    assert (await _invite(client, "manager")).status_code == 200

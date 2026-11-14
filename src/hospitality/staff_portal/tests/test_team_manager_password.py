"""Новый менеджер — только с паролем того, кто действует (#970, слово основателя 14.11).

Четыре двери нового менеджера — приглашение по одному и списком, «Новая ссылка» у
приглашения уровня «Менеджер» и смена уровня на него — без пароля и с неверным
паролем отказывают ERR-AUTH-017 и ничего не выпускают; неверный пароль тратит бюджет
входа (исчерпан — ERR-AUTH-006). Другие уровни пароля не спрашивают.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_invites, staff_team
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
TEAM_API = f"/staff/{HOTEL_SLUG}/api/team"
WRONG = "wrong-password!"

Door = Callable[[AsyncClient, str | None], Awaitable[httpx.Response]]


@pytest.fixture(autouse=True)
async def _reception(portal_hotel: PortalHotel) -> None:
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )


def _with(payload: Mapping[str, object], password: str | None) -> dict[str, object]:
    return dict(payload) if password is None else {**payload, "password": password}


async def _post(client: AsyncClient, path: str, payload: dict[str, object]) -> httpx.Response:
    return await client.post(f"{TEAM_API}{path}", json=payload, headers=SAME_ORIGIN)


def _invite_door(role: str) -> Door:
    services = [] if role == "manager" else ["reception"]

    async def door(client: AsyncClient, password: str | None) -> httpx.Response:
        payload = {
            "invited_name": "Новый",
            "login": unique_login(),
            "role_key": role,
            "service_keys": services,
        }
        return await _post(client, "/invites", _with(payload, password))

    return door


def _batch_door(role: str) -> Door:
    services = [] if role == "manager" else ["reception"]

    async def door(client: AsyncClient, password: str | None) -> httpx.Response:
        rows = [{"invited_name": f"Новый {n}", "login": unique_login()} for n in range(2)]
        payload = {"role_key": role, "service_keys": services, "rows": rows}
        return await _post(client, "/invites/batch", _with(payload, password))

    return door


def _reissue_door(invite_id: uuid.UUID) -> Door:
    async def door(client: AsyncClient, password: str | None) -> httpx.Response:
        return await _post(client, f"/invites/{invite_id}/reissue", _with({}, password))

    return door


def _raise_door(user_id: uuid.UUID) -> Door:
    async def door(client: AsyncClient, password: str | None) -> httpx.Response:
        payload = {"role_key": "manager", "service_keys": []}
        return await _post(client, f"/members/{user_id}/access", _with(payload, password))

    return door


def _password_refused(response: httpx.Response) -> bool:
    error = response.json().get("error") or {}
    reason = (error.get("details") or [{}])[0].get("reason")
    return response.status_code == 403 and (error.get("code"), reason) == (
        "ERR-AUTH-017",
        "password",
    )


async def _member_role(tenant_id: uuid.UUID, user_id: uuid.UUID) -> StaffRole:
    members = await staff_team.list_tenant_members(tenant_id)
    return next(member.role_key for member in members if member.user_id == user_id)


async def test_every_manager_door_needs_the_actors_password(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    tenant_id = portal_hotel.tenant_id
    pending = await staff_invites.create_invite(
        tenant_id, StaffRole.MANAGER, "Ждёт", unique_login(), invited_by=portal_hotel.user_id
    )
    senior = await create_staff_user(
        unique_login(), tenant_id=tenant_id, role=StaffRole.SENIOR, service_keys=("reception",)
    )
    doors = [
        _invite_door("manager"),
        _batch_door("manager"),
        _reissue_door(pending.invite_id),
        _raise_door(senior),
    ]
    await submit_login(client, portal_hotel.login)

    for password in (None, "", WRONG):
        for door in doors:
            assert _password_refused(await door(client, password)), (door, password)
    # Ничего не выпущено: одно прежнее приглашение, его ссылка жива, старший — старший.
    [still] = await staff_invites.list_pending_invites(tenant_id)
    assert still.invite_id == pending.invite_id
    assert await _member_role(tenant_id, senior) is StaffRole.SENIOR
    accepted = await second_client.post(
        f"/staff/invite/{pending.invite_token}", data={"password": PASSWORD}
    )
    assert accepted.status_code == 303

    fresh = await staff_invites.create_invite(
        tenant_id, StaffRole.MANAGER, "Ещё", unique_login(), invited_by=portal_hotel.user_id
    )
    doors[2] = _reissue_door(fresh.invite_id)
    for door in doors:
        assert (await door(client, PASSWORD)).status_code == 200, door
    assert await _member_role(tenant_id, senior) is StaffRole.MANAGER


async def test_other_levels_need_no_password(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    tenant_id = portal_hotel.tenant_id
    pending = await staff_invites.create_invite(
        tenant_id,
        StaffRole.SENIOR,
        "Старший",
        unique_login(),
        invited_by=portal_hotel.user_id,
        service_keys=["reception"],
    )
    staff = await create_staff_user(
        unique_login(), tenant_id=tenant_id, role=StaffRole.STAFF, service_keys=("reception",)
    )
    await submit_login(client, portal_hotel.login)

    for door in (_invite_door("senior"), _batch_door("staff"), _reissue_door(pending.invite_id)):
        assert (await door(client, None)).status_code == 200, door
    raised = await _post(
        client, f"/members/{staff}/access", {"role_key": "senior", "service_keys": ["reception"]}
    )
    assert raised.status_code == 200


async def test_wrong_password_spends_login_budget(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пароль перед новым менеджером — под бюджетами входа: подбирать его из незапертой
    сессии не дешевле, чем на странице входа."""
    await submit_login(client, portal_hotel.login)
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    shared = FakeRateLimitRedis()  # один на тест: счёт должен накапливаться
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: shared)
    get_settings.cache_clear()
    try:
        door = _invite_door("manager")
        for _ in range(2):
            assert _password_refused(await door(client, WRONG))
        exhausted = await door(client, PASSWORD)
        assert exhausted.status_code == 429
        assert exhausted.json()["error"]["code"] == "ERR-AUTH-006"
    finally:
        get_settings.cache_clear()
    assert await staff_invites.list_pending_invites(portal_hotel.tenant_id) == []


async def test_team_page_offers_password_for_manager(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await staff_invites.create_invite(
        portal_hotel.tenant_id,
        StaffRole.MANAGER,
        "Ждёт",
        unique_login(),
        invited_by=portal_hotel.user_id,
    )
    await submit_login(client, portal_hotel.login)

    page = (await client.get(f"/staff/{HOTEL_SLUG}/team")).text
    # Форма приглашения, списка и «Новая ссылка» приглашения менеджера.
    assert page.count("data-manager-password") >= 3
    assert 'data-manager-invite="1"' in page and "data-reissue-password" in page
    assert "Подтвердите своим паролем, что это делаете вы." in page

"""Страница «Сотрудники» (spec 0033 §7/§10, PR F серии #48; spec 0037 §4–§5).

Смоук страницы (роль `manager`, состав с логинами, ожидающие ссылки, «как
войти») + JSON-действия (пригласить с логином, отозвать, сменить роль,
отключить). Анонимная страница принятия приглашения — test_invite_pages.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    Tenant,
    TenantMembership,
    UserStatus,
)
from hospitality.platform.staff_invites import create_invite
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION, TenantMemberView
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal.team import _last_active_label, _status_label
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
TEAM_PAGE = f"/staff/{HOTEL_SLUG}/team"
TEAM_API = f"/staff/{HOTEL_SLUG}/api/team"


async def _json_post(
    client: AsyncClient, path: str, payload: dict[str, object] | None = None
) -> httpx.Response:
    """POST по CSRF-контракту JSON-действий: JSON-тип + same-origin Origin."""
    return await client.post(path, json=payload or {}, headers=SAME_ORIGIN)


async def _membership(user_id: uuid.UUID, tenant_id: uuid.UUID) -> TenantMembership:
    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.user_id == user_id, TenantMembership.tenant_id == tenant_id
            )
        )
    assert membership is not None
    return membership


# --------------------------------------------------------------------------
# Страница
# --------------------------------------------------------------------------


async def test_team_page_renders_for_manager(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await create_staff_user(
        "AIGU",
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Айгуль Горничная",
    )
    await submit_login(client, portal_hotel.login)

    response = await client.get(TEAM_PAGE)

    assert response.status_code == 200
    assert "Пригласить сотрудника" in response.text
    assert "Айгуль Горничная" in response.text
    assert ">AIGU<" in response.text  # логин в строке сотрудника (spec 0037 §5)
    assert "Аружан Менеджер" in response.text  # сам менеджер тоже в составе
    assert "это вы" in response.text
    assert "не заходил" in response.text
    assert 'name="login"' in response.text
    assert "Подставлен из имени — можно изменить." in response.text
    assert "приглашением под другим логином — реактивации нет" in response.text
    assert "без логина" not in response.text


async def test_team_page_tells_how_staff_log_in(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Spec 0037 §5: общий адрес, код отеля и прямая ссылка на кабинет —
    приглашение одноразовое, и без этой строки менеджеру неоткуда их взять."""
    await submit_login(client, portal_hotel.login)
    base = get_settings().public_base_url.rstrip("/")

    response = await client.get(TEAM_PAGE)

    assert f"Вход для сотрудников — {base}/staff, код отеля: <b>{HOTEL_SLUG}</b>." in (
        response.text
    )
    assert f'data-hotel-link="{base}/staff/{HOTEL_SLUG}"' in response.text
    assert "Скопировать ссылку" in response.text


async def test_member_without_login_is_marked(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Учётка без логина в этом отеле (переходный случай §6) помечается."""
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-elsewhere", name="Elsewhere")
        session.add(other)
        await session.flush()
        other_id = other.id
    member_id = await create_staff_user(unique_login(), tenant_id=other_id, role=StaffRole.STAFF)
    async with platform_session_scope() as session:
        session.add(
            TenantMembership(
                user_id=member_id, tenant_id=portal_hotel.tenant_id, role_key=StaffRole.STAFF
            )
        )
    await submit_login(client, portal_hotel.login)

    response = await client.get(TEAM_PAGE)

    assert "без логина — отключите и пригласите заново" in response.text


async def test_team_page_requires_manager_role(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Мини-матрица §3.2: ресепшен на страницу «Сотрудники» не проходит."""
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST
    )
    await submit_login(client, staff_login)

    response = await client.get(TEAM_PAGE)

    assert response.status_code == 403
    assert "Нет доступа" in response.text


async def test_team_page_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(TEAM_PAGE)
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/demo-hotel/login"


async def test_home_links_team_for_manager(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Раздел «Сотрудники» на главной перестал быть заглушкой «Скоро»."""
    await submit_login(client, portal_hotel.login)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert f'href="/staff/{HOTEL_SLUG}/team"' in response.text
    assert "Скоро" not in response.text


# --------------------------------------------------------------------------
# Приглашение: выпуск, показ, отзыв
# --------------------------------------------------------------------------


async def test_invite_link_is_issued_and_listed_then_revoked(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)

    created = await _json_post(
        client,
        f"{TEAM_API}/invites",
        {"invited_name": "Ерлан", "login": " erla ", "role_key": "receptionist"},
    )
    assert created.status_code == 200
    body = created.json()
    assert "/staff/invite/" in body["invite_url"]
    assert body["login"] == "ERLA"  # нормализован — так сотрудник и будет входить
    assert body["expires_in_hours"] == 72

    page = await client.get(TEAM_PAGE)
    assert "Ерлан" in page.text
    assert ">ERLA<" in page.text  # логин виден в строке ожидающего приглашения
    assert "Ожидают принятия" in page.text
    # Токен на страницу не возвращается: в БД только хэш, ссылка была один раз.
    assert body["invite_url"].rsplit("/", 1)[-1] not in page.text

    invite_id = page.text.split('data-invite-id="')[1].split('"')[0]
    revoked = await _json_post(client, f"{TEAM_API}/invites/{invite_id}/revoke")
    assert revoked.status_code == 200
    assert "Ожидают принятия" not in (await client.get(TEAM_PAGE)).text


async def test_invite_requires_manager_and_csrf(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST
    )
    await submit_login(client, staff_login)
    forbidden = await _json_post(
        client,
        f"{TEAM_API}/invites",
        {"invited_name": "Кто-то", "login": "KTOT", "role_key": "staff"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ERR-AUTH-003"

    # Тот же щит, что у остальных JSON-действий: без Origin — ERR-AUTH-009.
    await submit_login(client, portal_hotel.login)
    no_origin = await client.post(
        f"{TEAM_API}/invites", json={"invited_name": "Кто-то", "login": "KTOT", "role_key": "staff"}
    )
    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "ERR-AUTH-009"


async def test_invite_of_foreign_tenant_cannot_be_revoked(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Тенантная граница: id инвайта соседнего отеля неотличим от чужого."""
    async with platform_session_scope() as session:
        alien = Tenant(slug="hotel-alien", name="Alien Hotel")
        session.add(alien)
        await session.flush()
        alien_id = alien.id
    foreign = await create_invite(
        alien_id, StaffRole.STAFF, "Чужой", "CHUZ", invited_by=portal_hotel.user_id
    )
    await submit_login(client, portal_hotel.login)

    response = await _json_post(client, f"{TEAM_API}/invites/{foreign.invite_id}/revoke")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "ERR-AUTH-004"


async def test_invite_with_taken_or_malformed_login_is_refused(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Spec 0037 §4: занятый логин (учётка, ожидающее приглашение) → 409,
    не тот формат → 422 — оба ERR-AUTH-012; страница пишет свой текст у поля
    по статусу, ссылка не выпускается."""
    await submit_login(client, portal_hotel.login)
    issued = await _json_post(
        client,
        f"{TEAM_API}/invites",
        {"invited_name": "Дана", "login": "DANA", "role_key": "staff"},
    )
    assert issued.status_code == 200

    cases = ((portal_hotel.login, 409), ("dana", 409), ("ИЩКЬ", 422), ("", 422))
    for staff_login, status in cases:
        response = await _json_post(
            client,
            f"{TEAM_API}/invites",
            {"invited_name": "Другой", "login": staff_login, "role_key": "staff"},
        )
        assert response.status_code == status, staff_login
        assert response.json()["error"]["code"] == "ERR-AUTH-012"
    page = await client.get(TEAM_PAGE)
    assert page.text.count('data-action="revoke-invite"') == 1


# --------------------------------------------------------------------------
# Роли и отключение
# --------------------------------------------------------------------------


async def test_role_change_takes_effect_on_next_request(
    client: AsyncClient, portal_hotel: PortalHotel, second_client: AsyncClient
) -> None:
    staff_login = unique_login()
    member_id = await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(second_client, staff_login)
    assert (await second_client.get(f"/staff/{HOTEL_SLUG}/checkin")).status_code == 403

    await submit_login(client, portal_hotel.login)
    response = await _json_post(
        client, f"{TEAM_API}/members/{member_id}/role", {"role_key": "receptionist"}
    )

    assert response.status_code == 200
    assert (await _membership(member_id, portal_hotel.tenant_id)).role_key is (
        StaffRole.RECEPTIONIST
    )
    # Та же сессия сотрудника — уже с новой ролью, перелогин не нужен.
    assert (await second_client.get(f"/staff/{HOTEL_SLUG}/checkin")).status_code == 200


async def test_manager_cannot_change_own_role_or_deactivate_self(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """ERR-AUTH-011: самоблокировка последнего менеджера закрыта на платформе."""
    await submit_login(client, portal_hotel.login)

    demote = await _json_post(
        client, f"{TEAM_API}/members/{portal_hotel.user_id}/role", {"role_key": "staff"}
    )
    assert demote.status_code == 409
    assert demote.json()["error"]["code"] == ERR_AUTH_SELF_ACTION

    off = await _json_post(client, f"{TEAM_API}/members/{portal_hotel.user_id}/deactivate")
    assert off.status_code == 409
    assert off.json()["error"]["code"] == ERR_AUTH_SELF_ACTION
    assert (await client.get(TEAM_PAGE)).status_code == 200


async def test_deactivation_kills_session_and_revokes_membership(
    client: AsyncClient, portal_hotel: PortalHotel, second_client: AsyncClient
) -> None:
    staff_login = unique_login()
    member_id = await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(second_client, staff_login)
    assert (await second_client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200

    await submit_login(client, portal_hotel.login)
    response = await _json_post(client, f"{TEAM_API}/members/{member_id}/deactivate")

    assert response.status_code == 200
    # Сессия погашена немедленно (DoD #48), членство отозвано.
    dropped = await second_client.get(f"/staff/{HOTEL_SLUG}")
    assert dropped.status_code == 303
    assert dropped.headers["location"] == "/staff/demo-hotel/login"
    membership = await _membership(member_id, portal_hotel.tenant_id)
    assert membership.status is MembershipStatus.REVOKED
    assert "отключён" in (await client.get(TEAM_PAGE)).text


async def test_member_of_another_hotel_is_not_found(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Менеджер отеля A не трогает сотрудника отеля B, зная его user_id."""
    async with platform_session_scope() as session:
        alien = Tenant(slug="hotel-alien", name="Alien Hotel")
        session.add(alien)
        await session.flush()
        alien_id = alien.id
    stranger_id = await create_staff_user(unique_login(), tenant_id=alien_id, role=StaffRole.STAFF)
    await submit_login(client, portal_hotel.login)

    cases: tuple[tuple[str, dict[str, object] | None], ...] = (
        (f"{TEAM_API}/members/{stranger_id}/role", {"role_key": "manager"}),
        (f"{TEAM_API}/members/{stranger_id}/deactivate", None),
    )
    for path, payload in cases:
        response = await _json_post(client, path, payload)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ERR-AUTH-008"

    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == stranger_id)
        )
    assert membership is not None and membership.role_key is StaffRole.STAFF


@pytest.mark.parametrize("filename", ["styles.css", "queue.js", "checkin.js", "team.js"])
async def test_static_assets_are_served(client: AsyncClient, filename: str) -> None:
    response = await client.get(f"/staff/static/{filename}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_unknown_static_asset_is_404(client: AsyncClient) -> None:
    """Один маршрут статики отдаёт только перечисленные файлы: имя приходит
    из URL, но ищется в словаре, а не на диске (обхода каталога нет)."""
    assert (await client.get("/staff/static/unknown.js")).status_code == 404
    assert (await client.get("/staff/static/..%2F..%2F.env")).status_code == 404


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [(None, "не заходил"), (0, "сегодня"), (1, "вчера"), (5, "5 дн назад")],
)
def test_last_active_label(days_ago: int | None, expected: str) -> None:
    """Подписи активности — чистая функция: менеджеру важно «заходит / нет»."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    moment = None if days_ago is None else now - timedelta(days=days_ago)
    assert _last_active_label(moment, now) == expected


def test_status_label_separates_revoked_membership_from_deactivated_user() -> None:
    """Членство и учётка — две величины: отзыв членства (RBAC v1) не то же
    самое, что отключение (v1 гасит обе)."""

    def member(membership_status: MembershipStatus) -> TenantMemberView:
        return TenantMemberView(
            user_id=uuid.uuid4(),
            display_name="Кто-то",
            login="KTOT",
            role_key=StaffRole.STAFF,
            membership_status=membership_status,
            user_status=UserStatus.ACTIVE,
            last_active_at=None,
        )

    assert _status_label(member(MembershipStatus.REVOKED)) == "доступ отозван"
    assert _status_label(member(MembershipStatus.ACTIVE)) == "активен"


async def test_user_status_stays_consistent_after_deactivation(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    member_id = await create_staff_user(
        unique_login(), tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(client, portal_hotel.login)
    await _json_post(client, f"{TEAM_API}/members/{member_id}/deactivate")

    from hospitality.platform.staff_team import list_tenant_members

    members = await list_tenant_members(portal_hotel.tenant_id)
    dropped = next(member for member in members if member.user_id == member_id)
    assert dropped.user_status is UserStatus.DEACTIVATED
    assert dropped.membership_status is MembershipStatus.REVOKED
    # Отключённые уходят вниз списка — активные всегда сверху.
    assert members[-1].user_id == member_id

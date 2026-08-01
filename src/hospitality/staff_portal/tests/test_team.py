"""Страница «Сотрудники» и приглашения (spec 0033 §7/§10, PR F серии #48).

Смоук страницы (роль `manager`, состав, ожидающие ссылки) + JSON-действия
(пригласить, отозвать, сменить роль, отключить) + анонимный маршрут
`/staff/invite/{token}`: принятие ссылки заводит учётку и сразу пускает в
очередь, отработанная ссылка даёт один и тот же экран на все причины.
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
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_invites import create_invite
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION, TenantMemberView
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.staff_portal.team import _last_active_label, _status_label
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    submit_login,
)
from tests.test_staff_auth import PASSWORD, create_staff_user

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
        f"maid-{uuid.uuid4().hex[:8]}@hotel.kz",
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Айгуль Горничная",
    )
    await submit_login(client, portal_hotel.email)

    response = await client.get(TEAM_PAGE)

    assert response.status_code == 200
    assert "Пригласить сотрудника" in response.text
    assert "Айгуль Горничная" in response.text
    assert "Аружан Менеджер" in response.text  # сам менеджер тоже в составе
    assert "это вы" in response.text
    assert "не заходил" in response.text


async def test_team_page_requires_manager_role(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Мини-матрица §3.2: ресепшен на страницу «Сотрудники» не проходит."""
    email = f"front-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST)
    await submit_login(client, email)

    response = await client.get(TEAM_PAGE)

    assert response.status_code == 403
    assert "Нет доступа" in response.text


async def test_team_page_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(TEAM_PAGE)
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


async def test_home_links_team_for_manager(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Раздел «Сотрудники» на главной перестал быть заглушкой «Скоро»."""
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert f'href="/staff/{HOTEL_SLUG}/team"' in response.text
    assert "Скоро" not in response.text


# --------------------------------------------------------------------------
# Приглашение: выпуск, показ, отзыв
# --------------------------------------------------------------------------


async def test_invite_link_is_issued_and_listed_then_revoked(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.email)

    created = await _json_post(
        client, f"{TEAM_API}/invites", {"invited_name": "Ерлан", "role_key": "receptionist"}
    )
    assert created.status_code == 200
    body = created.json()
    assert "/staff/invite/" in body["invite_url"]
    assert body["expires_in_hours"] == 72

    page = await client.get(TEAM_PAGE)
    assert "Ерлан" in page.text
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
    email = f"front-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST)
    await submit_login(client, email)
    forbidden = await _json_post(
        client, f"{TEAM_API}/invites", {"invited_name": "Кто-то", "role_key": "staff"}
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ERR-AUTH-003"

    # Тот же щит, что у остальных JSON-действий: без Origin — ERR-AUTH-009.
    await submit_login(client, portal_hotel.email)
    no_origin = await client.post(
        f"{TEAM_API}/invites", json={"invited_name": "Кто-то", "role_key": "staff"}
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
        alien_id, StaffRole.STAFF, "Чужой", invited_by=portal_hotel.user_id
    )
    await submit_login(client, portal_hotel.email)

    response = await _json_post(client, f"{TEAM_API}/invites/{foreign.invite_id}/revoke")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "ERR-AUTH-004"


# --------------------------------------------------------------------------
# Роли и отключение
# --------------------------------------------------------------------------


async def test_role_change_takes_effect_on_next_request(
    client: AsyncClient, portal_hotel: PortalHotel, second_client: AsyncClient
) -> None:
    email = f"maid-{uuid.uuid4().hex[:8]}@hotel.kz"
    member_id = await create_staff_user(
        email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(second_client, email)
    assert (await second_client.get(f"/staff/{HOTEL_SLUG}/checkin")).status_code == 403

    await submit_login(client, portal_hotel.email)
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
    await submit_login(client, portal_hotel.email)

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
    email = f"leaver-{uuid.uuid4().hex[:8]}@hotel.kz"
    member_id = await create_staff_user(
        email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(second_client, email)
    assert (await second_client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200

    await submit_login(client, portal_hotel.email)
    response = await _json_post(client, f"{TEAM_API}/members/{member_id}/deactivate")

    assert response.status_code == 200
    # Сессия погашена немедленно (DoD #48), членство отозвано.
    dropped = await second_client.get(f"/staff/{HOTEL_SLUG}")
    assert dropped.status_code == 303
    assert dropped.headers["location"] == "/staff/login"
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
    stranger_id = await create_staff_user(
        f"alien-{uuid.uuid4().hex[:8]}@hotel.kz", tenant_id=alien_id, role=StaffRole.STAFF
    )
    await submit_login(client, portal_hotel.email)

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


# --------------------------------------------------------------------------
# Принятие приглашения (анонимная страница)
# --------------------------------------------------------------------------


async def test_invite_page_shows_hotel_role_and_consent(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Санжар", invited_by=portal_hotel.user_id
    )

    response = await client.get(f"/staff/invite/{grant.invite_token}")

    assert response.status_code == 200
    assert "Санжар" in response.text
    assert "Demo Hotel" in response.text
    assert "Сотрудник" in response.text
    # Согласие — та же строка, что видит гость (spec 0033 §3.4, consent v3).
    assert "обработкой персональных данных" in response.text
    assert "Политике конфиденциальности" in response.text


async def test_accepting_invite_creates_account_and_lands_in_queue(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Санжар", invited_by=portal_hotel.user_id
    )
    email = f"sanzhar-{uuid.uuid4().hex[:8]}@hotel.kz"

    response = await client.post(
        f"/staff/invite/{grant.invite_token}",
        data={"email": email, "password": PASSWORD},
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/requests"
    # Вошли сразу: cookie сессии уже в jar клиента.
    queue = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert queue.status_code == 200
    assert "Санжар" in (await client.get(f"/staff/{HOTEL_SLUG}")).text
    # Ссылка одноразовая — второй заход упирается в тот же экран.
    assert (await client.get(f"/staff/invite/{grant.invite_token}")).status_code == 410


async def test_used_expired_and_unknown_invites_share_one_screen(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Дана", invited_by=portal_hotel.user_id
    )
    await client.post(
        f"/staff/invite/{grant.invite_token}",
        data={"email": f"dana-{uuid.uuid4().hex[:8]}@hotel.kz", "password": PASSWORD},
    )

    for token in (grant.invite_token, "no-such-token"):
        page = await client.get(f"/staff/invite/{token}")
        assert page.status_code == 410
        assert "Ссылка больше не действует" in page.text
        posted = await client.post(
            f"/staff/invite/{token}",
            data={"email": f"x-{uuid.uuid4().hex[:8]}@hotel.kz", "password": PASSWORD},
        )
        assert posted.status_code == 410


async def test_invite_form_rejects_short_password_and_cross_origin(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Ким", invited_by=portal_hotel.user_id
    )
    path = f"/staff/invite/{grant.invite_token}"

    short = await client.post(
        path, data={"email": f"kim-{uuid.uuid4().hex[:8]}@hotel.kz", "password": "short"}
    )
    assert short.status_code == 422
    assert "не короче 8 символов" in short.text

    # CSRF-щит форм: принятие инвайта создаёт сессию, значит это login-CSRF.
    foreign = await client.post(
        path,
        data={"email": f"kim-{uuid.uuid4().hex[:8]}@hotel.kz", "password": PASSWORD},
        headers={"origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    # Инвайт не потреблён ни одной из отклонённых попыток.
    assert (await client.get(path)).status_code == 200


async def test_invite_does_not_reveal_existing_email(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Существующий email с неверным паролем отвечает тем же текстом, что
    форма входа: инвайт не должен становиться оракулом перечисления (PR #148).

    Второй кейс — блокер ревью PR #159: КОРОТКИЙ пароль обязан отвечать
    одинаковым СТАТУСОМ на занятый и на свободный email. Раньше 422 значило
    «email свободен», 401 — «занят», и различающая ветка была бесплатной.
    """
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.MANAGER, "Двойник", invited_by=portal_hotel.user_id
    )
    path = f"/staff/invite/{grant.invite_token}"

    response = await client.post(
        path, data={"email": portal_hotel.email, "password": "wrong-password-1"}
    )

    assert response.status_code == 401
    assert "Неверный email или пароль" in response.text

    taken = await client.post(path, data={"email": portal_hotel.email, "password": "short"})
    free = await client.post(
        path, data={"email": f"nobody-{uuid.uuid4().hex[:8]}@hotel.kz", "password": "short"}
    )
    assert taken.status_code == free.status_code == 422
    assert "не короче 8 символов" in taken.text
    assert "не короче 8 символов" in free.text
    # Ни одна проба ссылку не потребила — она всё ещё ждёт того, кому выписана.
    assert (await client.get(path)).status_code == 200


async def test_manager_accepting_own_invite_link_keeps_the_hotel(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """ERR-AUTH-011 закрыт и на третьем пути (блокер ревью PR #159): менеджер,
    решивший посмотреть свою же ссылку и введший свои email и пароль, раньше
    понижался до `staff` и запирал отель без единого менеджера."""
    await submit_login(client, portal_hotel.email)
    created = await _json_post(
        client, f"{TEAM_API}/invites", {"invited_name": "Новичок", "role_key": "staff"}
    )
    path = f"/staff/invite/{created.json()['invite_url'].rsplit('/', 1)[-1]}"

    response = await client.post(
        path, data={"email": portal_hotel.email, "password": PASSWORD}, headers=SAME_ORIGIN
    )

    assert response.status_code == 409
    assert "менеджер" in response.text
    assert (await _membership(portal_hotel.user_id, portal_hotel.tenant_id)).role_key is (
        StaffRole.MANAGER
    )
    # Кабинет на месте, ссылка цела — её примет тот, кому она выписана.
    assert (await client.get(TEAM_PAGE)).status_code == 200
    assert (await client.get(path)).status_code == 200


async def test_invite_accepted_but_login_failed_shows_page_not_json(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Рекомендация Р-1 ревью PR #159: вход после принятия — отдельная дверь со
    своим бюджетом попыток (он идёт и по IP, а отель сидит за одним NAT, #149).
    Учётка уже создана и ссылка потреблена, поэтому отказ входа обязан быть
    русской страницей «войдите сами», а не сырым JSON-конвертом."""
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Санжар", invited_by=portal_hotel.user_id
    )
    email = f"sanzhar-{uuid.uuid4().hex[:8]}@hotel.kz"

    async def _throttled(*args: object, **kwargs: object) -> object:
        raise AppError(
            code=ERR_AUTH_LOGIN_RATE_LIMITED,
            message="Too many login attempts — try again later",
            status_code=429,
        )

    monkeypatch.setattr("hospitality.platform.staff_auth.login", _throttled)
    response = await client.post(
        f"/staff/invite/{grant.invite_token}", data={"email": email, "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Учётная запись создана" in response.text
    assert "Слишком много попыток входа" in response.text
    assert "/staff/login" in response.text
    # Учётка действительно создана: паролем из формы человек войдёт сам.
    monkeypatch.undo()
    assert (await submit_login(client, email)).status_code == 303


async def test_deactivated_user_cannot_accept_invite(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    email = f"gone-{uuid.uuid4().hex[:8]}@hotel.kz"
    member_id = await create_staff_user(
        email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(client, portal_hotel.email)
    await _json_post(client, f"{TEAM_API}/members/{member_id}/deactivate")
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Вернулся", invited_by=portal_hotel.user_id
    )

    response = await client.post(
        f"/staff/invite/{grant.invite_token}", data={"email": email, "password": PASSWORD}
    )

    assert response.status_code == 403
    assert "деактивирована" in response.text
    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == member_id)
        )
    assert membership is not None and membership.status is MembershipStatus.REVOKED


async def test_invite_route_is_not_a_tenant_slug(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`invite` — служебный сегмент кабинета (`_STAFF_RESERVED_SEGMENTS`):
    страница приглашения работает без сессии и без контекста тенанта."""
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, "Аноним", invited_by=portal_hotel.user_id
    )
    response = await client.get(f"/staff/invite/{grant.invite_token}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


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
        f"leaver-{uuid.uuid4().hex[:8]}@hotel.kz",
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
    )
    await submit_login(client, portal_hotel.email)
    await _json_post(client, f"{TEAM_API}/members/{member_id}/deactivate")

    from hospitality.platform.staff_team import list_tenant_members

    members = await list_tenant_members(portal_hotel.tenant_id)
    dropped = next(member for member in members if member.user_id == member_id)
    assert dropped.user_status is UserStatus.DEACTIVATED
    assert dropped.membership_status is MembershipStatus.REVOKED
    # Отключённые уходят вниз списка — активные всегда сверху.
    assert members[-1].user_id == member_id

"""«Сотрудники»: истёкшее приглашение в списке и «Новая ссылка» (spec 0037 §4, #944).

Страница показывает истёкшее с пометкой, «Новой ссылкой» и «Убрать»; действие
отдаёт новую ссылку один раз, прежняя гаснет; логин, ушедший другому, — 409.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from httpx import AsyncClient

from hospitality.platform.models import StaffInvite, StaffRole
from hospitality.platform.staff_invites import create_invite, describe_invite
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login

SAME_ORIGIN = {"origin": "https://test"}
TEAM_PAGE = f"/staff/{HOTEL_SLUG}/team"
TEAM_API = f"/staff/{HOTEL_SLUG}/api/team"


async def _expire(invite_id: object) -> None:
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id)
        assert invite is not None
        invite.expires_at = utc_now() - timedelta(hours=2)


async def test_expired_invite_is_listed_and_reissued(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    live = await create_invite(
        portal_hotel.tenant_id, StaffRole.MANAGER, "Дана", "DANA", invited_by=portal_hotel.user_id
    )
    expired = await create_invite(
        portal_hotel.tenant_id, StaffRole.MANAGER, "Ерлан", "ERLA", invited_by=portal_hotel.user_id
    )
    await _expire(expired.invite_id)
    await submit_login(client, portal_hotel.login)

    page = (await client.get(TEAM_PAGE)).text
    assert page.count('data-action="reissue-invite"') == 2
    assert "ссылка истекла" in page
    assert ">Убрать</button>" in page and ">Отозвать</button>" in page

    response = await client.post(
        f"{TEAM_API}/invites/{expired.invite_id}/reissue", json={}, headers=SAME_ORIGIN
    )

    assert response.status_code == 200
    body = response.json()
    assert body["login"] == "ERLA"
    token = urlsplit(body["invite_url"]).path.rsplit("/", 1)[-1]
    invitation = await describe_invite(token)
    assert invitation is not None and invitation.invited_name == "Ерлан"
    page = (await client.get(TEAM_PAGE)).text
    assert "ссылка истекла" not in page
    assert page.count(">Отозвать</button>") == 2

    # Живую ссылку новая гасит.
    again = await client.post(
        f"{TEAM_API}/invites/{live.invite_id}/reissue", json={}, headers=SAME_ORIGIN
    )
    assert again.status_code == 200
    assert await describe_invite(live.invite_token) is None


async def test_reissue_with_taken_login_is_409(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    expired = await create_invite(
        portal_hotel.tenant_id, StaffRole.MANAGER, "Ли", "LIAN", invited_by=portal_hotel.user_id
    )
    await _expire(expired.invite_id)
    await create_invite(
        portal_hotel.tenant_id, StaffRole.MANAGER, "Ли Ан", "LIAN", invited_by=portal_hotel.user_id
    )
    await submit_login(client, portal_hotel.login)
    response = await client.post(
        f"{TEAM_API}/invites/{expired.invite_id}/reissue", json={}, headers=SAME_ORIGIN
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR-AUTH-012"

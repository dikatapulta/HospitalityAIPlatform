"""Анонимная страница приглашения `/staff/invite/{token}` (spec 0033 §3.4, §10;
spec 0037 §4, §7, §10): логин задал менеджер, сотрудник придумывает только
пароль; принятие заводит учётку, сразу пускает в очередь и запоминает отель
браузера; отработанная ссылка даёт один и тот же экран на все причины.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import resolve_staff_session
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_invites import create_invite
from hospitality.shared.errors import AppError
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD

SAME_ORIGIN = {"origin": "https://test"}


async def _invite_path(portal_hotel: PortalHotel, name: str, staff_login: str) -> str:
    grant = await create_invite(
        portal_hotel.tenant_id, StaffRole.STAFF, name, staff_login, invited_by=portal_hotel.user_id
    )
    return f"/staff/invite/{grant.invite_token}"


async def test_invite_page_shows_login_hotel_code_role_and_consent(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    path = await _invite_path(portal_hotel, "Санжар", "SANZ")

    response = await client.get(path)

    assert response.status_code == 200
    assert "Санжар, вас приглашают в кабинет персонала" in response.text
    assert "Demo Hotel" in response.text
    assert "Сотрудник" in response.text
    assert "Ваш логин — <b>SANZ</b>" in response.text
    assert f"код отеля — <b>{HOTEL_SLUG}</b>" in response.text
    assert "Придумайте пароль — с логином и паролем вы будете входить в кабинет." in (response.text)
    assert 'name="password"' in response.text
    assert 'name="email"' not in response.text
    # Согласие — та же строка, что видит гость (spec 0033 §3.4, consent v3).
    assert "обработкой персональных данных" in response.text
    assert "Политике конфиденциальности" in response.text


async def test_accepting_invite_creates_account_and_lands_in_queue(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    path = await _invite_path(portal_hotel, "Санжар", "SANZ")

    response = await client.post(path, data={"password": PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/requests"
    # Вошли сразу: cookie сессии уже в jar клиента, отель браузер запомнил.
    assert (await client.get(f"/staff/{HOTEL_SLUG}/requests")).status_code == 200
    assert "Санжар" in (await client.get(f"/staff/{HOTEL_SLUG}")).text
    assert client.cookies.get("staff_hotel") == HOTEL_SLUG
    # Ссылка одноразовая — второй заход упирается в тот же экран.
    assert (await client.get(path)).status_code == 410
    # Дальше сотрудник входит логином из приглашения.
    await client.post("/staff/logout")
    assert (await submit_login(client, "sanz")).status_code == 303


async def test_accepting_invite_revokes_previous_browser_session(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Автовход после принятия — такой же успешный вход (spec 0037 §3):
    прежняя сессия браузера отзывается, а не доживает до TTL без владельца."""
    await submit_login(client, portal_hotel.login)
    previous_token = client.cookies.get("staff_session")
    assert previous_token is not None
    path = await _invite_path(portal_hotel, "Новичок", "NOVI")

    response = await client.post(path, data={"password": PASSWORD}, headers=SAME_ORIGIN)

    assert response.status_code == 303
    assert await resolve_staff_session(previous_token) is None


async def test_used_expired_and_unknown_invites_share_one_screen(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    path = await _invite_path(portal_hotel, "Дана", "DANA")
    await client.post(path, data={"password": PASSWORD})

    for target in (path, "/staff/invite/no-such-token"):
        page = await client.get(target)
        assert page.status_code == 410
        assert "Ссылка больше не действует" in page.text
        posted = await client.post(target, data={"password": PASSWORD})
        assert posted.status_code == 410


async def test_invite_form_rejects_empty_and_short_password_and_cross_origin(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    path = await _invite_path(portal_hotel, "Ким", "KIMA")

    empty = await client.post(path, data={})
    assert empty.status_code == 422
    assert "Введите пароль." in empty.text

    short = await client.post(path, data={"password": "short"})
    assert short.status_code == 422
    assert "не короче 8 символов" in short.text

    # CSRF-щит форм: принятие инвайта создаёт сессию, значит это login-CSRF.
    foreign = await client.post(
        path, data={"password": PASSWORD}, headers={"origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    # Инвайт не потреблён ни одной из отклонённых попыток.
    assert (await client.get(path)).status_code == 200


async def test_invite_accepted_but_login_failed_shows_page_not_json(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Рекомендация Р-1 ревью PR #159: вход после принятия — отдельная дверь со
    своим бюджетом попыток (он идёт и по IP, а отель сидит за одним NAT, #207).
    Учётка уже создана и ссылка потреблена, поэтому отказ входа обязан быть
    русской страницей «войдите сами» с логином и входом отеля, а не сырым
    JSON-конвертом."""
    path = await _invite_path(portal_hotel, "Санжар", "SANZ")

    async def _throttled(*args: object, **kwargs: object) -> object:
        raise AppError(
            code=ERR_AUTH_LOGIN_RATE_LIMITED,
            message="Too many login attempts — try again later",
            status_code=429,
        )

    monkeypatch.setattr("hospitality.platform.staff_auth.login", _throttled)
    response = await client.post(path, data={"password": PASSWORD})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Учётная запись создана" in response.text
    assert "Слишком много неудачных попыток входа" in response.text
    assert "SANZ и паролем, которые только что задали" in " ".join(response.text.split())
    assert f'href="/staff/{HOTEL_SLUG}/login"' in response.text
    # Учётка действительно создана: логином и паролем человек войдёт сам.
    monkeypatch.undo()
    assert (await submit_login(client, "SANZ")).status_code == 303


async def test_invite_route_is_not_a_tenant_slug(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`invite` — служебный сегмент кабинета (`_STAFF_RESERVED_SEGMENTS`):
    страница приглашения работает без сессии и без контекста тенанта."""
    response = await client.get(await _invite_path(portal_hotel, "Аноним", "ANON"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

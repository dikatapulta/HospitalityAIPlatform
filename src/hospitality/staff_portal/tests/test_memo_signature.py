"""«Служебные записки» → «Моя подпись» и плашка о привязке (spec 0077 §4, §9, §12 PR 4б; #936).

Флаг отеля выключен — пункта меню нет, страница и действия 404. Привязка — по HTTP целиком:
пароль → параметры → ответ программного аутентификатора (проверяет настоящая библиотека
`webauthn`) → название устройства из `User-Agent` запроса. Вход без «Запомнить меня» —
текст вместо формы и отказ ERR-AUTH-018. Плашку видит другой вход владельца, а не
привязавший; «Это не я» — только с паролем и гасит остальные входы. Образец подписи —
рисунком из целых чисел. «Сбросить подпись» в «Сотрудниках» — менеджеру, при записках.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from hospitality.app import create_app
from hospitality.modules.memos import api as memos_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_passkeys import list_passkeys
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal import memos, passkey_notice_api
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from tests.conftest import TEST_FILE_ENCRYPTION_KEY, FakeRateLimitRedis
from tests.passkey_authenticator import IPHONE_SAFARI_UA, SoftAuthenticator
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
PAGE = f"{HOME}/memos/signature"
API = f"{HOME}/api/memos/signature"
SAME_ORIGIN = {"origin": "https://test"}
PHONE = {**SAME_ORIGIN, "user-agent": IPHONE_SAFARI_UA}
STROKES = [[[10, 20], [300, 200], [600, 120]], [[700, 300]]]


@pytest.fixture
def shared_redis() -> Iterator[FakeRateLimitRedis]:
    """Один фейк Redis на тест: вызов привязки живёт между двумя запросами (conftest
    кабинета даёт каждому вызову новый фейк — для лимитов входа этого хватало).
    И тестовый ключ шифрования: образец подписи модуль хранит шифртекстом."""
    fake = FakeRateLimitRedis()
    patcher = pytest.MonkeyPatch()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    patcher.setenv("FILE_ENCRYPTION_KEY", TEST_FILE_ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield fake
    patcher.undo()
    get_settings.cache_clear()


@pytest.fixture
async def app(shared_redis: FakeRateLimitRedis) -> FastAPI:
    return create_app()


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with await _client(app) as http:
        yield http


async def _hotel(tenant_id: uuid.UUID, *, enabled: bool = True) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                memos_enabled=enabled,
            ),
        )


async def _staff(hotel: PortalHotel, role: StaffRole = StaffRole.STAFF) -> tuple[str, uuid.UUID]:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=hotel.tenant_id, role=role, display_name="Әсел"
    )
    return staff_login, user_id


async def _login(client: AsyncClient, staff_login: str, *, remember: bool = True) -> None:
    response = await client.post(
        f"{HOME}/login",
        data={"login": staff_login, "password": PASSWORD, "remember": "1" if remember else ""},
    )
    assert response.status_code == 303


async def _bind(client: AsyncClient, device: SoftAuthenticator) -> str:
    begun = await client.post(f"{API}/bind/begin", json={"password": PASSWORD}, headers=PHONE)
    assert begun.status_code == 200, begun.text
    finished = await client.post(
        f"{API}/bind/finish",
        json={"credential": device.register(begun.json()["options"])},
        headers=PHONE,
    )
    assert finished.status_code == 200, finished.text
    label: str = finished.json()["device_label"]
    return label


def _error(response: Response) -> tuple[str, str | None]:
    body = response.json()["error"]
    details = body.get("details") or [{}]
    return body["code"], details[0].get("reason")


async def test_section_off_hides_menu_page_and_actions(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id, enabled=False)
    staff_login, _ = await _staff(portal_hotel)
    await _login(client, staff_login)

    home = await client.get(HOME)
    assert memos.NAV_TITLE not in home.text
    assert (await client.get(PAGE)).status_code == 404
    begun = await client.post(f"{API}/bind/begin", json={"password": PASSWORD}, headers=PHONE)
    assert begun.status_code == 404


async def test_bind_this_device_over_http(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, user_id = await _staff(portal_hotel)
    await _login(client, staff_login)

    page = await client.get(PAGE)
    assert page.status_code == 200
    assert memos.SIGNATURE_INTRO in page.text and memos.NAV_TITLE in page.text
    assert "data-bind-form" in page.text and memos.DEVICES_EMPTY in page.text

    assert await _bind(client, SoftAuthenticator()) == "iPhone · Safari"
    [device] = await list_passkeys(user_id)
    assert device.device_label == "iPhone · Safari"
    page = await client.get(PAGE)
    assert "iPhone · Safari · привязано" in page.text and memos.DEVICES_EMPTY not in page.text


async def test_bind_refusals(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, _ = await _staff(portal_hotel)
    await _login(client, staff_login, remember=False)

    page = await client.get(PAGE)
    assert memos.NOT_REMEMBERED_TEXT in page.text and "data-bind-form" not in page.text
    refused = await client.post(f"{API}/bind/begin", json={"password": PASSWORD}, headers=PHONE)
    assert refused.status_code == 403 and _error(refused) == ("ERR-AUTH-018", "not_remembered")

    await _login(client, staff_login)
    wrong = await client.post(f"{API}/bind/begin", json={"password": "wrong-pass"}, headers=PHONE)
    assert wrong.status_code == 403 and _error(wrong) == ("ERR-AUTH-017", "password")
    no_origin = await client.post(f"{API}/bind/begin", json={"password": PASSWORD})
    assert no_origin.status_code == 403 and _error(no_origin)[0] == "ERR-AUTH-009"


async def test_revoke_own_device_needs_password(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, user_id = await _staff(portal_hotel)
    await _login(client, staff_login)
    await _bind(client, SoftAuthenticator())
    [device] = await list_passkeys(user_id)
    url = f"{API}/devices/{device.id}/revoke"

    wrong = await client.post(url, json={"password": "wrong-pass"}, headers=SAME_ORIGIN)
    assert wrong.status_code == 403 and await list_passkeys(user_id)
    revoked = await client.post(url, json={"password": PASSWORD}, headers=SAME_ORIGIN)
    assert revoked.json() == {"revoked": True} and await list_passkeys(user_id) == []


async def test_signature_sample_is_drawn_from_numbers(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, _ = await _staff(portal_hotel)
    await _login(client, staff_login)

    saved = await client.post(f"{API}/sample", json={"strokes": STROKES}, headers=SAME_ORIGIN)
    assert saved.json() == {"ok": True}
    page = await client.get(PAGE)
    assert '<polyline points="10,20 300,200 600,120"/>' in page.text
    assert '<polyline points="700,300"/>' in page.text and "data-sample-redo" in page.text

    outside = [[[10, 20], [1001, 5]]]
    refused = await client.post(f"{API}/sample", json={"strokes": outside}, headers=SAME_ORIGIN)
    assert refused.status_code == 422


async def test_banner_is_for_other_logins_and_disown_needs_password(
    app: FastAPI, client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, user_id = await _staff(portal_hotel)
    await _login(client, staff_login)
    await _bind(client, SoftAuthenticator())
    [device] = await list_passkeys(user_id)
    # Привязавший вход плашки не видит (ревью PR #943).
    assert "data-passkey-notice" not in (await client.get(HOME)).text

    async with await _client(app) as owner:
        await _login(owner, staff_login)
        home = await owner.get(HOME)
        assert "«iPhone · Safari»" in home.text and "Это не я" in home.text
        assert passkey_notice_api.DISOWN_HINT in home.text
        url = f"{HOME}/api/passkey-notice/{device.id}/disown"
        wrong = await owner.post(url, json={"password": "wrong-pass"}, headers=SAME_ORIGIN)
        assert wrong.status_code == 403 and await list_passkeys(user_id)

        disowned = await owner.post(url, json={"password": PASSWORD}, headers=SAME_ORIGIN)
        assert disowned.json() == {"ok": True} and await list_passkeys(user_id) == []
        assert "data-passkey-notice" not in (await owner.get(HOME)).text
    # Остальные входы учётки погашены — привязавший выходит на страницу входа.
    assert (await client.get(PAGE)).status_code == 303


async def test_acknowledge_hides_banner(
    app: FastAPI, client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, user_id = await _staff(portal_hotel)
    await _login(client, staff_login)
    await _bind(client, SoftAuthenticator())
    [device] = await list_passkeys(user_id)
    url = f"{HOME}/api/passkey-notice/{device.id}/ack"
    # Привязавший вход плашку закрыть не может.
    assert (await client.post(url, json={}, headers=SAME_ORIGIN)).json() == {"ok": False}

    async with await _client(app) as owner:
        await _login(owner, staff_login)
        assert (await owner.post(url, json={}, headers=SAME_ORIGIN)).json() == {"ok": True}
        assert "data-passkey-notice" not in (await owner.get(HOME)).text
        # Закрыта «Это я» — отречься уже нельзя (аудит #949, В2-4).
        disown = f"{HOME}/api/passkey-notice/{device.id}/disown"
        late = await owner.post(disown, json={"password": PASSWORD}, headers=SAME_ORIGIN)
        assert late.json() == {"ok": False} and await list_passkeys(user_id)


async def test_manager_resets_signature_in_team(
    app: FastAPI, client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_login, user_id = await _staff(portal_hotel)
    await _login(client, staff_login)
    await _bind(client, SoftAuthenticator())
    await client.post(f"{API}/sample", json={"strokes": STROKES}, headers=SAME_ORIGIN)
    reset = f"{HOME}/api/team/members/{user_id}/signature-reset"
    # Сотрудник чужую подпись не сбрасывает.
    assert (await client.post(reset, json={}, headers=SAME_ORIGIN)).status_code == 403

    async with await _client(app) as manager:
        await _login(manager, portal_hotel.login)
        team = await manager.get(f"{HOME}/team")
        assert 'data-action="reset-signature"' in team.text
        done = await manager.post(reset, json={}, headers=SAME_ORIGIN)
        assert done.json() == {"passkeys_revoked": 1}
    assert await list_passkeys(user_id) == []
    page = await client.get(PAGE)
    assert "<polyline" not in page.text and "data-sample-redo" not in page.text


async def test_team_has_no_signature_reset_without_memos(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id, enabled=False)
    _, user_id = await _staff(portal_hotel)
    await _login(client, portal_hotel.login)

    team = await client.get(f"{HOME}/team")
    assert team.status_code == 200 and 'data-action="reset-signature"' not in team.text
    reset = f"{HOME}/api/team/members/{user_id}/signature-reset"
    assert (await client.post(reset, json={}, headers=SAME_ORIGIN)).status_code == 404


def test_sample_points_are_integers_only() -> None:
    sample = memos_api.SignatureStrokes.model_validate({"strokes": STROKES})
    assert memos.strokes_points(sample) == ["10,20 300,200 600,120", "700,300"]

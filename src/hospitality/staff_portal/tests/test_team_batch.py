"""«Пригласить списком» на странице «Сотрудники» (spec 0072 §4, §9 PR 2): маршрут
`POST /staff/{slug}/api/team/invites/batch` — права, щит, «весь список или ни одного»
с номерами строк, предел 100, лист для печати без кэша, принятие обычной страницей.
Правила списка — tests/test_staff_invite_batch.py."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import ERR_AUTH_INVALID_SERVICES
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import team_invites_api
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD, RECEPTION, create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
BATCH = f"/staff/{HOTEL_SLUG}/api/team/invites/batch"
TEAM_PAGE = f"/staff/{HOTEL_SLUG}/team"


async def _make_services(portal_hotel: PortalHotel) -> None:
    with tenant_context(portal_hotel.tenant_id):
        for key, name in (("reception", "Ресепшен"), ("housekeeping", "Уборка и бельё")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )


def _body(*rows: tuple[str, str], role: str = "staff", services: list[str] | None = None) -> Any:
    return {
        "role_key": role,
        "service_keys": ["housekeeping"] if services is None else services,
        "rows": [{"invited_name": name, "login": login} for name, login in rows],
    }


async def _post(client: AsyncClient, payload: Any) -> httpx.Response:
    return await client.post(BATCH, json=payload, headers=SAME_ORIGIN)


@pytest.fixture
def qr_carries_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """QR — картинка; чтобы тест дошёл до ссылки, «QR» здесь — сама ссылка текстом."""
    monkeypatch.setattr(team_invites_api, "qr_svg", lambda url: f"<svg>{url}</svg>")


async def test_manager_gets_print_sheet_and_invites_are_pending(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)

    response = await _post(client, _body(("Айгуль Сейтова", "aysa"), ("Иван Петров", "IVAP")))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    sheet = response.json()
    assert re.fullmatch(r"\d\d\.\d\d, \d\d:\d\d", sheet["expires_local"])
    assert [(card["invited_name"], card["login"]) for card in sheet["cards"]] == [
        ("Айгуль Сейтова", "AYSA"),
        ("Иван Петров", "IVAP"),
    ]
    assert all(card["qr_svg"].startswith("<svg") for card in sheet["cards"])
    assert "invite_url" not in sheet["cards"][0]  # ссылка — только картинкой
    page = (await client.get(TEAM_PAGE)).text
    assert page.count('data-action="revoke-invite"') == 2
    assert ">AYSA<" in page and ">IVAP<" in page


async def test_link_from_the_sheet_is_accepted_by_the_usual_page(
    client: AsyncClient, portal_hotel: PortalHotel, qr_carries_url: None
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)
    sheet = (await _post(client, _body(("Санжар", "SANZ")))).json()
    url = sheet["cards"][0]["qr_svg"].removeprefix("<svg>").removesuffix("</svg>")
    path = url[url.index("/staff/invite/") :]
    await client.post("/staff/logout")

    accepted = await client.post(path, data={"password": PASSWORD})

    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/staff/{HOTEL_SLUG}/requests"


async def test_bad_rows_issue_nothing_and_come_back_with_numbers(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)

    response = await _post(
        client,
        _body(
            ("Хороший", "GOOD"),
            ("Свой", portal_hotel.login),
            ("Кириллица", "ИЩКЬ"),
            ("Повтор", "good"),
        ),
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "ERR-AUTH-012"
    assert error["details"] == [
        {"row": 2, "reason": "taken"},
        {"row": 3, "reason": "format"},
        {"row": 4, "reason": "duplicate"},
    ]
    assert 'data-action="revoke-invite"' not in (await client.get(TEAM_PAGE)).text


async def test_list_size_and_blank_names_are_refused_by_schema(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)
    too_many = [(f"Имя {index}", f"L{index:04d}") for index in range(101)]

    for payload in (_body(*too_many), _body(), _body(("   ", "BLNK"))):
        response = await _post(client, payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ERR-PLATFORM-002"
    assert 'data-action="revoke-invite"' not in (await client.get(TEAM_PAGE)).text


async def test_services_are_checked_once_for_the_whole_list(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)

    refused = await _post(client, _body(("Нет", "NETS"), services=[]))
    manager = await _post(client, _body(("Босс", "BOSS"), role="manager", services=list(RECEPTION)))

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == ERR_AUTH_INVALID_SERVICES
    assert manager.status_code == 200


@pytest.mark.parametrize("role", [StaffRole.STAFF, StaffRole.SENIOR])
async def test_only_manager_issues_and_only_through_the_shield(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole
) -> None:
    await _make_services(portal_hotel)
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=RECEPTION
    )
    await submit_login(client, staff_login)

    forbidden = await _post(client, _body(("Кто-то", "KTOT")))

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ERR-AUTH-003"

    await client.post("/staff/logout")
    await submit_login(client, portal_hotel.login)
    no_origin = await client.post(BATCH, json=_body(("Кто-то", "KTOT")))
    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "ERR-AUTH-009"


async def test_tokens_and_names_stay_out_of_logs(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    qr_carries_url: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _make_services(portal_hotel)
    await submit_login(client, portal_hotel.login)
    configure_logging()
    capsys.readouterr()

    sheet = (await _post(client, _body(("Zarina Markerova", "ZMARK")))).json()

    token = sheet["cards"][0]["qr_svg"].removesuffix("</svg>").rsplit("/", 1)[-1]
    output = capsys.readouterr().out
    assert "staff.invite_batch_created" in output
    for secret in (token, "Markerova", "ZMARK"):
        assert secret not in output


async def test_team_page_has_batch_form_sheet_and_scripts(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Тексты spec 0072 §8 — в шаблоне; логины на странице помечены для подсказки,
    общий скрипт грузится раньше обоих скриптов страницы."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(TEAM_PAGE)).text

    assert "Пригласить списком" in page
    assert "Имена — по одному в строке" in page
    assert "За раз — до 100 человек." in page
    assert "Составить список" in page
    assert "Выпустить приглашения" in page
    assert "Ссылки видны только сейчас — распечатайте и раздайте каждому лично." in page
    assert "Demo Hotel · кабинет сотрудника" in page
    assert "1. Наведите камеру телефона на код и придумайте пароль." in page
    assert f"<b>{HOTEL_SLUG}</b>, логин <b data-card-login></b>." in page
    assert "Не передавайте листок другим." in page
    assert f'data-known-login="{portal_hotel.login}"' in page
    scripts = re.findall(r'src="/staff/static/(team\w*\.js)', page)
    assert scripts == ["team_common.js", "team.js", "team_batch.js"]
    for script in scripts:
        assert (await client.get(f"/staff/static/{script}")).status_code == 200

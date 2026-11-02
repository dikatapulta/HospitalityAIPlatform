"""Окна заявки кабинета v3 (#636): «Новая заявка» поверх страниц отеля и
карточка заявки панелью в очереди.

Поведение окон — в браузере (app_windows.js, new_request.js, queue.js); здесь —
то, что держит сервер: окно есть на каждой странице отеля, кроме самой формы;
поля оно берёт фрагментом при открытии, и в разметке страниц служб отеля нет
(сводка и журнал старшего не называют чужих служб, spec 0045 §3); ссылки
«Новая заявка» остаются ссылками на страницу (без JS — обычный переход);
служба, заявку которой сотрудник взять не может, помечена заранее; у карточки
очереди есть всё для панели — шапка, кнопка-номер и время создания по часам
отеля. Создание и «взять» — те же JSON-действия, их тесты — test_new_request.py
и test_queue.py.
"""

from __future__ import annotations

import re
import uuid
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
QUEUE = f"/staff/{HOTEL_SLUG}/requests"
FORM = f"/staff/{HOTEL_SLUG}/requests/new"
FIELDS = f"/staff/{HOTEL_SLUG}/requests/new/fields"


async def _category(tenant_id: uuid.UUID, key: str, name: str) -> requests_api.RequestCategoryRead:
    with tenant_context(tenant_id):
        return await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name=name)
        )


def _dialog(html: str) -> str:
    match = re.search(
        r'<div class="app-overlay[^"]*" id="new-request-dialog".*?</section>', html, re.S
    )
    assert match is not None, "no new request dialog"
    return match.group(0)


async def test_every_hotel_page_offers_the_new_request_dialog(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    await submit_login(client, portal_hotel.login)

    for path in (HOME, QUEUE):
        html = (await client.get(path)).text
        dialog = _dialog(html)
        assert 'data-new-request-form data-mode="dialog"' in dialog, path
        assert f'data-fields-url="{FIELDS}"' in dialog, path
        # Службы — только во фрагменте полей, не в каждой странице.
        assert "Уборка" not in dialog, path
        assert "/staff/static/app_windows.js" in html, path
        assert html.count("/staff/static/new_request.js") == 1, path
    # «+» телефона — ссылка на страницу формы: без JS она просто ведёт туда.
    assert f'class="app-fab" href="{FORM}" data-new-request-open' in html
    queue_html = (await client.get(QUEUE)).text
    assert f'href="{FORM}" data-new-request-open' in queue_html


async def test_dialog_fields_come_as_a_fragment(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    await submit_login(client, portal_hotel.login)

    response = await client.get(FIELDS)

    assert response.status_code == 200
    assert "<html" not in response.text  # фрагмент — без каркаса страницы
    assert 'id="dialog-room"' in response.text
    assert "Уборка" in response.text
    assert "Сразу взять себе" in response.text


async def test_dialog_fields_without_services_say_what_to_do(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get(FIELDS)
    assert response.status_code == 200
    assert "Службы отеля ещё не заведены" in response.text
    assert 'name="category_id"' not in response.text


async def test_dialog_fields_without_session_redirect_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(FIELDS)
    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/login"


async def test_form_page_keeps_its_own_form_without_the_dialog(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Страница — для прямых ссылок: одна форма, второй поверх неё не нужно."""
    await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    await submit_login(client, portal_hotel.login)

    html = (await client.get(FORM)).text

    assert 'id="new-request-dialog"' not in html
    assert 'data-new-request-form data-mode="page"' in html
    assert html.count("/staff/static/new_request.js") == 1
    assert "Сразу взять себе" in html


async def test_foreign_services_are_marked_for_the_claim_checkbox(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Сотрудник уборки создаёт заявку в ремонт, но взять её не может (spec 0045 §3)."""
    own = await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    foreign = await _category(portal_hotel.tenant_id, "maintenance", "Ремонт")
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=("housekeeping",),
    )
    await submit_login(client, staff_login)

    fields = (await client.get(FIELDS)).text

    assert re.search(rf'value="{own.id}"\s+data-service-name="Уборка">', fields)
    assert re.search(rf'value="{foreign.id}"\s+data-service-name="Ремонт" data-foreign>', fields)


async def test_manager_may_claim_in_every_service(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    await _category(portal_hotel.tenant_id, "maintenance", "Ремонт")
    await submit_login(client, portal_hotel.login)
    assert "data-foreign" not in (await client.get(FIELDS)).text


async def test_queue_card_carries_the_request_panel(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    category = await _category(portal_hotel.tenant_id, "housekeeping", "Уборка")
    with tenant_context(portal_hotel.tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="второе полотенце",
                room_number="1207",
            )
        )
    await submit_login(client, portal_hotel.login)

    html = (await client.get(QUEUE)).text

    assert 'id="request-panel" data-window hidden' in html
    assert f'data-panel-title="Заявка #{created.daily_number}"' in html
    assert 'data-panel-sub="Комната 1207 · Уборка"' in html
    assert f'aria-label="Открыть заявку #{created.daily_number}"' in html
    # «Создана» — по часам отеля (Asia/Almaty, UTC+5), а не по UTC сервера.
    almaty = created.created_at.astimezone(ZoneInfo("Asia/Almaty"))
    assert f"<dt>Создана</dt><dd>{almaty:%d.%m %H:%M}</dd>" in html
    assert "<dt>Исполнитель</dt><dd>Не назначен</dd>" in html
    assert "data-search-input" in html  # Ctrl+K / ⌘K — поле поиска очереди

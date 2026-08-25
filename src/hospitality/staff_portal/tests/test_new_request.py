"""Форма «Новая заявка» кабинета (spec 0035 §5/§13 блок «Форма», PR C серии #299).

Страница ручного приёма (три поля, службы чипсами) и её JSON-действие: заявка
рождается тем же путём, что у гостя, — с дневным номером, событием
`request.created` (из него растёт уведомление в чат службы) и `origin`, по
которому потом считается доля Exit-критерия Phase 1. Пустые поля дают отказ
текстом, а не 500; чужой тенант ни страницы, ни действия не видит.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole, Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.events import OutboxEvent
from hospitality.shared.metrics import staff_manual_requests_total
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user

SAME_ORIGIN = {"origin": "https://test"}

FORM_PATH = f"/staff/{HOTEL_SLUG}/requests/new"
CREATE_PATH = f"/staff/{HOTEL_SLUG}/api/requests"


async def _make_category(
    tenant_id: uuid.UUID, *, key: str = "housekeeping", name: str = "Уборка"
) -> requests_api.RequestCategoryRead:
    with tenant_context(tenant_id):
        return await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name=name)
        )


async def _submit(
    client: AsyncClient, payload: dict[str, str], path: str = CREATE_PATH
) -> httpx.Response:
    """POST формы по CSRF-контракту JSON-действий (JSON-тело + same-origin Origin)."""
    return await client.post(path, json=payload, headers=SAME_ORIGIN)


# ---------------------------------------------------------------------------
# Страница формы
# ---------------------------------------------------------------------------


async def test_form_page_renders_three_fields_and_service_chips(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_category(portal_hotel.tenant_id)
    await _make_category(portal_hotel.tenant_id, key="maintenance", name="Ремонт")
    await submit_login(client, portal_hotel.email)

    response = await client.get(FORM_PATH)

    assert response.status_code == 200
    assert "Номер комнаты" in response.text
    assert "Что нужно сделать" in response.text
    assert "Создать заявку" in response.text
    assert "Уборка" in response.text  # службы — чипсами по категориям тенанта
    assert "Ремонт" in response.text
    assert "/staff/static/new_request.js" in response.text


async def test_queue_header_links_to_the_form(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Вход в форму — из очереди: единственное место, где сотрудник ищет заявки."""
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert "+ Новая заявка" in response.text
    assert FORM_PATH in response.text


async def test_form_page_without_categories_offers_no_form(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Отель без служб (онбординг не завершён) страницу не роняет — подсказывает."""
    await submit_login(client, portal_hotel.email)
    response = await client.get(FORM_PATH)
    assert response.status_code == 200
    assert "Службы отеля ещё не заведены" in response.text
    assert "Создать заявку" not in response.text


async def test_form_page_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(FORM_PATH)
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


# ---------------------------------------------------------------------------
# Создание заявки
# ---------------------------------------------------------------------------


async def test_manual_request_gets_daily_number_and_staff_manual_origin(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Заявка рождается тем же путём, что у гостя (P-5): номер `#N`, событие, origin.

    Уведомление в чат службы растёт из `request.created` — подписчик Telegram
    его и слушает, поэтому строка в outbox и есть проверка «уведомление ушло»:
    самого подписчика тесты канала проверяют отдельно.
    """
    category = await _make_category(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    response = await _submit(
        client,
        {
            "room_number": "1207",
            "category_id": str(category.id),
            "summary": "Гость позвонил на ресепшен: просит второе полотенце",
        },
    )

    assert response.status_code == 200
    created = response.json()
    assert created["daily_number"] is not None
    assert created["origin"] == "staff_manual"
    assert created["room_number"] == "1207"
    assert created["summary"] == "Гость позвонил на ресепшен: просит второе полотенце"
    assert created["status"] == "new"
    # Язык гостя на этом пути неизвестен, срочность формой не спрашивается (§10).
    assert created["guest_language"] is None
    assert created["is_urgent"] is False

    async with platform_session_scope() as session:
        rows = await session.execute(
            select(OutboxEvent).where(OutboxEvent.event_name == "request.created")
        )
    (event_row,) = rows.scalars().all()
    assert event_row.payload["request_id"] == created["id"]


async def test_manual_request_appears_in_the_queue_with_a_flash(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Возврат в очередь: заявка в ленте, плашка называет её номер (§9)."""
    category = await _make_category(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    created = (
        await _submit(
            client,
            {
                "room_number": "1207",
                "category_id": str(category.id),
                "summary": "второе полотенце",
            },
        )
    ).json()

    response = await client.get(
        f"/staff/{HOTEL_SLUG}/requests", params={"created": created["daily_number"]}
    )

    assert f"Заявка #{created['daily_number']} создана" in response.text
    assert "второе полотенце" in response.text


async def test_queue_ignores_a_bogus_created_parameter(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Номер в плашке приходит из URL — то есть от кого угодно: чужой текст туда
    не попадает, а страница всё равно открывается (канон `parse_queue_tab`)."""
    await submit_login(client, portal_hotel.email)
    for bogus in ("<b>2</b>", "999999", "не число", ""):
        response = await client.get(f"/staff/{HOTEL_SLUG}/requests", params={"created": bogus})
        assert response.status_code == 200, bogus
        assert "создана —" not in response.text, bogus


async def test_manual_request_counts_in_the_metric(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Счётчик ручного приёма (§12) — прямое мерило доли Ф-1."""
    category = await _make_category(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    label = str(portal_hotel.tenant_id)
    before = staff_manual_requests_total.labels(tenant_id=label)._value.get()

    await _submit(
        client,
        {"room_number": "1207", "category_id": str(category.id), "summary": "полотенце"},
    )

    assert staff_manual_requests_total.labels(tenant_id=label)._value.get() == before + 1


@pytest.mark.parametrize("role", [StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER])
async def test_every_role_may_accept_a_request_manually(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole
) -> None:
    """Роль любая из трёх (docs/RBAC.md): звонок принимает ресепшен, а горничную
    ловят в коридоре — ограничивать здесь нечего."""
    category = await _make_category(portal_hotel.tenant_id)
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=role)
    await submit_login(client, email)

    page = await client.get(FORM_PATH)
    created = await _submit(
        client,
        {"room_number": "1207", "category_id": str(category.id), "summary": "полотенце"},
    )

    assert page.status_code == 200, role
    assert created.status_code == 200, role


# ---------------------------------------------------------------------------
# Отказы: пустые поля, чужая служба, чужой тенант, CSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("room_number", ""), ("room_number", "   "), ("summary", ""), ("summary", "  \n ")],
)
async def test_blank_fields_are_rejected_with_an_error_not_a_500(
    client: AsyncClient, portal_hotel: PortalHotel, field: str, value: str
) -> None:
    """Пустое поле — каноническим конвертом отказа (§13), а не 500 и не заявкой
    без сути. Тексты трёх полей показывает форма (`new_request.js`), сервер
    держит ту же границу схемой."""
    category = await _make_category(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    payload = {"room_number": "1207", "category_id": str(category.id), "summary": "полотенце"}
    payload[field] = value

    response = await _submit(client, payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-PLATFORM-002"
    with tenant_context(portal_hotel.tenant_id):
        assert await requests_api.list_open_requests(limit=10) == []


async def test_unknown_category_is_rejected(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Служба чужого отеля и несуществующая неразличимы (RLS, P-4) — обе 404."""
    await submit_login(client, portal_hotel.email)
    response = await _submit(
        client,
        {"room_number": "1207", "category_id": str(uuid.uuid4()), "summary": "полотенце"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-REQUESTS-001"


async def test_foreign_tenant_sees_neither_the_form_nor_the_action(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Роль доказывает право В СВОЁМ отеле: у чужого slug закрыты обе двери —
    страница и действие (иначе одна из них дыра, README «Новое право»)."""
    async with platform_session_scope() as session:
        session.add(Tenant(slug="hotel-alien", name="Alien Hotel"))
    await submit_login(client, portal_hotel.email)

    page = await client.get("/staff/hotel-alien/requests/new")
    action = await _submit(
        client,
        {"room_number": "1207", "category_id": str(uuid.uuid4()), "summary": "полотенце"},
        path="/staff/hotel-alien/api/requests",
    )

    assert page.status_code == 403
    assert "Нет доступа" in page.text
    assert action.status_code == 403


async def test_create_action_requires_the_csrf_contract(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Тот же щит, что у остальных JSON-действий: JSON-тип + непустой same-origin
    Origin, иначе 403 ERR-AUTH-009 (кросс-сайтовая форма JSON-тип не умеет)."""
    category = await _make_category(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)
    payload = {"room_number": "1207", "category_id": str(category.id), "summary": "полотенце"}

    without_origin = await client.post(CREATE_PATH, json=payload)
    foreign_origin = await client.post(
        CREATE_PATH, json=payload, headers={"origin": "https://evil.example"}
    )

    assert without_origin.status_code == 403
    assert without_origin.json()["error"]["code"] == "ERR-AUTH-009"
    assert foreign_origin.status_code == 403
    with tenant_context(portal_hotel.tenant_id):
        assert await requests_api.list_open_requests(limit=10) == []

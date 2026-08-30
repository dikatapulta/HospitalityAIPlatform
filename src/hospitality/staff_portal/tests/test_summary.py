"""Страница «Сводка дня» (spec 0035 §7/§9, issue #300).

Смоук страницы: роль, надписи §9, переключатель «Сегодня / Вчера», пустой день,
подпись плитки «Создано» с третьим источником и тенант без конфигурации. Сами
числа проверены там, где их считают (`modules/requests/tests/test_day_summary.py`,
`channels/common/tests/test_escalations.py`) — здесь предмет другой: что
менеджер увидит глазами.
"""

from __future__ import annotations

import uuid
from datetime import date

from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import summary
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user

SUMMARY_PAGE = f"/staff/{HOTEL_SLUG}/summary"


async def _make_request(
    tenant_id: uuid.UUID,
    *,
    summary: str,
    origin: requests_api.ServiceRequestOrigin,
) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get("housekeeping")
        if category is None:
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
            )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id, origin=origin, summary=summary
            )
        )


def test_median_label_reads_like_a_person_wrote_it() -> None:
    """§9 не называет медиану медианой — плитка показывает «6 мин», а подпись
    объясняет её словами. Три порядка величины по канону `queue._age_label`;
    прочерк — когда в этот день не брали (мерить нечего, а не ноль)."""
    assert summary._median_label(40) == "40 сек"
    assert summary._median_label(6 * 60) == "6 мин"
    assert summary._median_label(2 * 60 * 60) == "2 ч"
    assert summary._median_label(None) == "—"


def test_day_label_uses_the_genitive_month() -> None:
    """«29 августа», а не «29 август»: `%B` дал бы именительный падеж и зависел
    бы от локали процесса, которой в контейнере нет."""
    assert summary._day_label(date(2026, 8, 29)) == "29 августа"
    assert summary._day_label(date(2026, 1, 1)) == "1 января"


async def test_summary_page_requires_manager_role(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Мини-матрица docs/RBAC.md: сводка дня — только менеджеру. Ресепшен видит
    ту же страницу «Нет доступа», что и на «Сотрудниках»."""
    email = f"front-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST)
    await submit_login(client, email)

    response = await client.get(SUMMARY_PAGE)

    assert response.status_code == 403
    assert "Нет доступа" in response.text


async def test_summary_page_shows_tiles_and_services(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Надписи §9 на месте, разрез «По службам» — под плитками. Подпись плитки
    «Создано» без заявок через публичную дверь двузначна: «через бота · вручную»
    (issue #313 — третий источник печатается только ненулевым)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(
        portal_hotel.tenant_id,
        summary="убрать 305",
        origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
    )
    await _make_request(
        portal_hotel.tenant_id,
        summary="позвонили на ресепшен",
        origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
    )
    await submit_login(client, portal_hotel.email)

    response = await client.get(SUMMARY_PAGE)

    assert response.status_code == 200
    body = response.text
    for label in (
        "Сводка дня",
        "Создано",
        "Закрыто",
        "Открыто сейчас",
        "Берут в работу",
        "Просрочено за день",
        "Бот звал сотрудника",
        "По службам",
        "Уборка",
    ):
        assert label in body
    assert "через бота 1 · вручную 1" in body
    assert "из внешней системы" not in body


async def test_summary_page_names_the_third_source_when_it_exists(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """issue #313: появилась заявка через публичную дверь — она названа, и сумма
    подписи сходится с «Создано». Пока источников печаталось два, менеджер читал
    бы несходящуюся арифметику как ошибку счёта."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(
        portal_hotel.tenant_id,
        summary="от гостя",
        origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
    )
    await _make_request(
        portal_hotel.tenant_id,
        summary="из интеграции",
        origin=requests_api.ServiceRequestOrigin.API,
    )
    await submit_login(client, portal_hotel.email)

    response = await client.get(SUMMARY_PAGE)

    assert "через бота 1 · вручную 0 · из внешней системы 1" in response.text


async def test_summary_page_opens_for_a_tenant_without_config(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """spec 0035 §13: незавершённый онбординг (и служебный smoke-тенант) страницу
    не роняет. Числа модуля свой прочерк уже умеют, но у страницы ВТОРОЕ,
    независимое чтение конфига — часовой пояс дня (`checkin.hotel_zone`), — и
    падало бы именно оно. `portal_hotel` конфига не пишет: здесь его просто не
    ставят, в отличие от остальных тестов файла.
    """
    await _make_request(
        portal_hotel.tenant_id,
        summary="отель без онбординга",
        origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
    )
    await submit_login(client, portal_hotel.email)

    response = await client.get(SUMMARY_PAGE)

    assert response.status_code == 200
    body = response.text
    assert "через бота 1" in body  # день посчитан, а не подменён заглушкой
    assert "напоминания выключены" in body  # сроков у отеля без конфига нет


async def test_empty_day_says_so_instead_of_zeroes(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§9: день, в котором не случилось вообще ничего, — одна фраза, а не шесть
    нулей. Переключатель при этом остаётся: с него уходят на другой день."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    response = await client.get(f"{SUMMARY_PAGE}?day=yesterday")

    assert response.status_code == 200
    assert "За этот день заявок не было." in response.text
    assert "Вчера" in response.text


async def test_open_now_keeps_the_day_from_being_empty(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§9: предикат пустого дня — ВСЕ числа нули. Вчера ничего не создавали и не
    закрывали, но открытая заявка висит — значит день не пуст, и менеджер видит
    её в плитке «Открыто сейчас», а не фразу «заявок не было»."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(
        portal_hotel.tenant_id,
        summary="висит открытой",
        origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
    )
    await submit_login(client, portal_hotel.email)

    response = await client.get(f"{SUMMARY_PAGE}?day=yesterday")

    assert "За этот день заявок не было." not in response.text
    assert "Открыто сейчас" in response.text


async def test_unknown_day_parameter_opens_today(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Канон `parse_queue_tab`: опечатка в ссылке от коллеги не даёт 422 —
    страница открывается на сегодняшнем дне."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.email)

    response = await client.get(f"{SUMMARY_PAGE}?day=позавчера")

    assert response.status_code == 200
    assert 'href="/staff/demo-hotel/summary"' in response.text


async def test_summary_link_is_on_the_home_page_for_manager_only(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Раздел на главной виден по мини-матрице: у менеджера есть, у горничной
    нет — иначе она нашла бы ссылку и упёрлась в «Нет доступа»."""
    await submit_login(client, portal_hotel.email)
    manager_home = await client.get(f"/staff/{HOTEL_SLUG}")

    email = f"maid-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(second_client, email)
    staff_home = await second_client.get(f"/staff/{HOTEL_SLUG}")

    assert "Сводка дня" in manager_home.text
    assert "Сводка дня" not in staff_home.text

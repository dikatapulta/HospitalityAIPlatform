"""Лист «Отчёт» (spec 0083 §6 пп. 2–3; issue #950).

Числа заявок считает `requests.period_summary` — сценарии в
`modules/requests/tests/test_period_summary.py`. Здесь — то, что добавляет страница:
разбор периода, права и выключатель, блоки по разделам отеля, прочерк вне срока
хранения, сравнение с прошлым периодом.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.report_period import (
    CUSTOM_ERROR,
    PERIOD_CUSTOM,
    PERIOD_MONTH,
    PERIOD_WEEK,
    parse_report_period,
    range_label,
)
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_mentor import _hotel as mentor_hotel
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/report"
HOME = f"/staff/{HOTEL_SLUG}"
ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture(autouse=True)
def _report_enabled() -> Iterator[None]:
    """Раздел включён во всех тестах файла — канон `test_stats`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_MANAGER_REPORT", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


def test_week_and_month_are_previous_calendar_periods() -> None:
    """§2.2: в понедельник и в воскресенье «прошлая неделя» — одна и та же
    законченная неделя; прошлый месяц в январе — декабрь прошлого года."""
    for today in (date(2026, 11, 9), date(2026, 11, 15)):
        week = parse_report_period({}, today=today)
        assert (week.key, week.first_day, week.last_day) == (
            PERIOD_WEEK,
            date(2026, 11, 2),
            date(2026, 11, 8),
        )
        assert (week.previous_first, week.previous_last) == (date(2026, 10, 26), date(2026, 11, 1))

    month = parse_report_period({"period": PERIOD_MONTH}, today=date(2026, 1, 20))
    assert (month.first_day, month.last_day) == (date(2025, 12, 1), date(2025, 12, 31))
    assert (month.previous_first, month.previous_last) == (date(2025, 11, 1), date(2025, 11, 30))


def test_custom_dates_compare_with_as_many_days_before() -> None:
    """§2.2: свои даты включительно; сравнение — столько же дней прямо перед ними.
    93 дня, конец в будущем и мусор — прошлая неделя с надписью, без 422."""
    today = date(2026, 11, 11)
    custom = parse_report_period(
        {"period": PERIOD_CUSTOM, "from": "2026-11-01", "to": "2026-11-10"}, today=today
    )
    assert (custom.key, custom.error) == (PERIOD_CUSTOM, None)
    assert (custom.previous_first, custom.previous_last) == (date(2026, 10, 22), date(2026, 10, 31))

    for first, last in (("2026-08-01", "2026-11-01"), ("2026-11-10", "2026-11-12"), ("x", "")):
        wrong = parse_report_period(
            {"period": PERIOD_CUSTOM, "from": first, "to": last}, today=today
        )
        assert (wrong.key, wrong.error) == (PERIOD_WEEK, CUSTOM_ERROR)
    assert parse_report_period({"period": "year"}, today=today).key == PERIOD_WEEK


def test_range_label_words() -> None:
    assert range_label(date(2026, 11, 2), date(2026, 11, 8)) == "2–8 ноября 2026"
    assert range_label(date(2026, 10, 26), date(2026, 11, 1)) == "26 октября – 1 ноября 2026"
    assert range_label(date(2025, 12, 29), date(2026, 1, 4)) == "29 декабря 2025 – 4 января 2026"
    assert range_label(date(2026, 11, 5), date(2026, 11, 5)) == "5 ноября 2026"


async def test_page_is_for_manager_only_and_hidden_when_switched_off(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2.5: менеджер видит лист и пункт меню; старший — 403 и без ссылки.
    Выключатель снят — 404 и пункта нет."""
    await submit_login(client, portal_hotel.login)
    page = await client.get(PAGE)
    assert page.status_code == 200
    assert "Отчёт<span" in page.text
    assert "За этот период заявок не было." in page.text
    assert "Распечатать или сохранить в PDF" in page.text
    assert f'href="{PAGE}"' in (await client.get(HOME)).text

    senior_login = unique_login()
    await create_staff_user(
        senior_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.SENIOR,
        display_name="Старшая",
        service_keys=("housekeeping",),
    )
    await submit_login(second_client, senior_login)
    assert (await second_client.get(PAGE)).status_code == 403
    assert f'href="{PAGE}"' not in (await second_client.get(HOME)).text

    monkeypatch.setenv("ENABLE_MANAGER_REPORT", "false")
    get_settings.cache_clear()
    assert (await client.get(PAGE)).status_code == 404
    assert f'href="{PAGE}"' not in (await client.get(HOME)).text


async def _done_request(tenant_id: uuid.UUID, category_id: uuid.UUID, day: date) -> None:
    """Заявка, созданная в полдень `day` отеля, взятая через 5 мин и выполненная через 30."""
    created_at = datetime.combine(day, time(12), tzinfo=ALMATY)
    with tenant_context(tenant_id):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="guest request",
                room_number="305",
            )
        )
        await requests_api.change_request_status(created.id, requests_api.RequestStatus.IN_PROGRESS)
        await requests_api.change_request_status(created.id, requests_api.RequestStatus.DONE)
        # ORM заявки — приватная часть модуля (контракт import-linter), поэтому SQL.
        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE service_requests SET service_day = :day, created_at = :at,"
                    " claimed_at = :claimed, closed_at = :closed WHERE id = :id"
                ),
                {
                    "day": day,
                    "at": created_at,
                    "claimed": created_at + timedelta(minutes=5),
                    "closed": created_at + timedelta(minutes=30),
                    "id": created.id,
                },
            )


def _cells(html: str, name: str) -> list[list[str]]:
    """Ячейки строки службы `name` словами без тегов: число периода, затем прошлого."""
    match = re.search(rf"<tr><td>{re.escape(name)}</td>(.*?)</tr>", html, re.S)
    assert match is not None, name
    cells = re.findall(r"<td>(.*?)</td>", match.group(1))
    return [re.sub(r"<[^>]+>", " ", cell).split() for cell in cells]


async def test_numbers_of_last_week_with_previous_week_and_optional_blocks(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§2.3: прошлая неделя рядом с позапрошлой; оценки — только при включённых
    оценках отеля; идей и экзамена у отеля без разделов нет; вопросы без ответа —
    всегда, число и ссылка на справочник."""
    await store_hotel_config(
        portal_hotel.tenant_id, guest_feedback_enabled=True, escalation_category_key="reception"
    )
    with tenant_context(portal_hotel.tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
    week = parse_report_period({}, today=utc_now().astimezone(ALMATY).date())
    await _done_request(portal_hotel.tenant_id, category.id, week.first_day)
    await _done_request(portal_hotel.tenant_id, category.id, week.last_day)
    await _done_request(portal_hotel.tenant_id, category.id, week.previous_first)

    await submit_login(client, portal_hotel.login)
    html = (await client.get(PAGE)).text
    assert range_label(week.first_day, week.last_day) in html
    assert _cells(html, "Уборка") == [
        ["2", "1"],
        ["2", "1"],
        ["0", "0"],
        ["0", "0"],
        ["5", "мин", "5", "мин"],
        ["30", "мин", "30", "мин"],
    ]
    assert "было 1" in html
    assert "Оценки гостей" in html
    assert "Идеи сотрудников" not in html
    assert "Экзамен «Наставника»" not in html
    assert "Вопросы гостей без ответа" in html
    assert f'href="/staff/{HOTEL_SLUG}/knowledge"' in html


async def test_old_custom_period_shows_dash_for_deleted_questions(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§2.3 п. 5: вопросы живут 90 дней — у окна старше прочерк и причина, а не ноль.
    Ошибка в датах — прошлая неделя с надписью."""
    await store_hotel_config(portal_hotel.tenant_id)
    today = utc_now().astimezone(ALMATY).date()
    first, last = today - timedelta(days=200), today - timedelta(days=190)
    await submit_login(client, portal_hotel.login)
    html = (
        await client.get(PAGE, params={"period": "custom", "from": str(first), "to": str(last)})
    ).text
    assert range_label(first, last) in html
    assert "вопросы хранятся 90 дней" in html

    wrong = (await client.get(PAGE, params={"period": "custom", "from": "x", "to": "y"})).text
    assert CUSTOM_ERROR in wrong


async def test_exam_block_shows_service_lines_without_names(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.3 п. 3: «Наставник» включён и есть назначение — строки служб словами «Итогов
    экзаменов», без имён сотрудников; оценок и идей у отеля нет — их блоков нет."""
    # Кодекс грузится с эмбеддингами Fake-провайдера — назначения нужны настроенные
    # (канон `test_mentor._mentor_models`); в OpenAI вызов не уходит.
    monkeypatch.setenv("LLM_MODEL_MENTOR", "gpt-6-luna")
    monkeypatch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-sent")
    get_settings.cache_clear()
    await mentor_hotel(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        await mentor_api.create_exam_assignment(
            chapter_numbers=None, service_keys=["housekeeping"], created_by_user_id=None
        )
    await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Горничная Айгуль",
        service_keys=("housekeeping",),
    )

    await submit_login(client, portal_hotel.login)
    html = (await client.get(PAGE)).text
    assert "Экзамен «Наставника» — на" in html
    assert "Уборка — 1 сотрудник · не проходили 1" in html
    assert "Горничная Айгуль" not in html
    assert "Оценки гостей" not in html
    assert "Идеи сотрудников" not in html

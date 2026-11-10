"""Главная кабинета v3 (issue #635, PR 2; до неё — #454, #462).

Что проверяется: карточки по ролям (мини-матрица docs/RBAC.md); числа очереди
— открытые, новые / в работе, просрочено тем же `queue.is_overdue`, что у
карточки очереди и сигнала; строка-итог; вкладки «Требует внимания» и строки в
них; «Взять себе» тем же действием, что у очереди; заезды и выезды по местному
дню отеля (граница — полночь Алматы, а не UTC); приветствие по часам отеля;
главная отеля без конфига и без срока ответа; «К передаче смены» — только у
кого открыт раздел кассы. Часы двигаются подменой `home.utc_now` — канон
тестов очереди: в проде стареет заявка, а не подделывается её `created_at`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.humanize import plural
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import checkin, home
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
HOTEL_ZONE = ZoneInfo("Asia/Almaty")
SAME_ORIGIN = {"origin": "https://test"}


@pytest.fixture
def handover_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Касса включена (демо-выключатель) — канон `demo_sections_enabled` test_shell."""
    monkeypatch.setenv("ENABLE_DEMO_HANDOVER", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _make_request(
    tenant_id: uuid.UUID, summary: str, room: str = "305"
) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get("housekeeping") or await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number=room,
            )
        )


async def _check_in(tenant_id: uuid.UUID, room: str) -> guests_api.StayRead:
    """Заселение на одну ночь: выезд — завтра 12:00 по Алматы (правило кабинета)."""
    with tenant_context(tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=room,
                check_out_at=checkin.checkout_at_utc(
                    checkin.HotelTiming(zone=HOTEL_ZONE), 1, now=utc_now()
                ),
            )
        )
    return checked.stay


async def _login_as(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...] = (),
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=role,
        service_keys=service_keys,
        display_name="Санжар Нурланов",
    )
    await submit_login(client, staff_login)


def _metric(html: str, key: str) -> int:
    match = re.search(rf'data-metric="{key}">(\d+)<', html)
    assert match is not None, f"no metric {key}"
    return int(match.group(1))


def _legend(html: str, tone: str) -> int:
    match = re.search(rf'data-legend="{tone}">.*?<strong>(\d+)</strong>', html)
    assert match is not None, f"no legend {tone}"
    return int(match.group(1))


def _tab_count(html: str, key: str) -> int | None:
    match = re.search(
        rf'class="tab home-tab" data-home-tab="{key}".*?home-count[^"]*">(\d+)<', html
    )
    return int(match.group(1)) if match else None


def _rows(html: str) -> list[tuple[str, str, str, str]]:
    """Строки «Требует внимания»: (вкладки, суть, метка состояния, текст метки)."""
    return re.findall(
        r'class="home-row" data-tabs="([^"]+)".*?</span>([^<]+)</a>.*?data-status="(\w+)">([^<]+)<',
        html,
        re.S,
    )


def _local_midnight_after(moment: datetime) -> datetime:
    local = moment.astimezone(HOTEL_ZONE)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), HOTEL_ZONE)
    return midnight.astimezone(UTC)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (4, "Доброй ночи"),
        (5, "Доброе утро"),
        (11, "Доброе утро"),
        (12, "Добрый день"),
        (17, "Добрый день"),
        (18, "Добрый вечер"),
        (22, "Добрый вечер"),
        (23, "Доброй ночи"),
        (None, "Здравствуйте"),
    ],
)
def test_greeting_follows_the_hotel_hour(hour: int | None, expected: str) -> None:
    assert home.greeting(hour) == expected


def test_date_label_first_name_and_plural() -> None:
    assert home.date_label(date(2026, 10, 19)) == "Понедельник, 19 октября"
    assert home.date_label(date(2026, 10, 25)) == "Воскресенье, 25 октября"
    assert home.first_name("Аружан Менеджер") == "Аружан"
    assert home.first_name("Санжар") == "Санжар"
    forms = ("заявка", "заявки", "заявок")
    assert [plural(n, *forms) for n in (0, 1, 2, 5, 11, 12, 21, 22, 111, 104)] == [
        "заявок", "заявка", "заявки", "заявок", "заявок",
        "заявок", "заявка", "заявки", "заявок", "заявки",
    ]  # fmt: skip


async def test_staff_role_sees_only_request_cards(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Горничная и техник — только заявки: заселение им закрыто, и страница
    не показывает (и не считает) его чисел; кассы у них нет."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("housekeeping",))
    response = await client.get(HOME)
    assert response.status_code == 200
    for shown in ('data-kpi="late"', 'data-kpi="open"'):
        assert shown in response.text
    assert 'data-kpi="today"' not in response.text
    assert 'data-kpi="turnovers"' not in response.text  # доска выключена (spec 0046 §5)
    assert "К передаче смены" not in response.text
    for hidden in ("Сводка дня", "Справочник отеля", "Журнал изменений", "Сотрудники"):
        assert hidden not in response.text


async def test_receptionist_sees_today_card_but_no_manager_sections(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)
    response = await client.get(HOME)
    assert 'data-kpi="open"' in response.text
    assert 'data-kpi="today"' in response.text
    for hidden in ("Сводка дня", "Справочник отеля", "Журнал изменений", "Сотрудники"):
        assert hidden not in response.text


async def test_manager_sees_every_section_with_links(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    for path in ("requests", "requests/new", "checkin", "summary", "knowledge", "changes", "team"):
        assert f'href="{HOME}/{path}"' in response.text
    assert "Аружан Менеджер" in response.text  # карточка меню: кто вошёл
    assert "К выбору отеля" not in response.text  # выбор отеля — в каркасе (#635, PR 1)


async def test_queue_numbers_summary_and_tabs(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Открытые, новые / в работе, просрочено — то же, что увидит открывший
    очередь. Просрочка считается тем же `queue.is_overdue`: взятая заявка не
    просрочена, сколько бы ни висела; закрытой на главной нет."""
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _make_request(portal_hotel.tenant_id, "убрать 305", room="305")
    await _make_request(portal_hotel.tenant_id, "полотенца", room="412")
    taken = await _make_request(portal_hotel.tenant_id, "лампа", room="118")
    done = await _make_request(portal_hotel.tenant_id, "вода", room="220")
    with tenant_context(portal_hotel.tenant_id):
        for request, status in (
            (taken, requests_api.RequestStatus.IN_PROGRESS),
            (done, requests_api.RequestStatus.IN_PROGRESS),
            (done, requests_api.RequestStatus.DONE),
        ):
            await requests_api.change_request_status(request.id, status)
    await submit_login(client, portal_hotel.login)

    fresh = (await client.get(HOME)).text
    assert _metric(fresh, "open") == 3
    assert (_legend(fresh, "new"), _legend(fresh, "progress")) == (2, 1)
    assert '<progress class="home-bar" value="2" max="3"' in fresh
    assert _metric(fresh, "late") == 0
    assert "home-kpi-warn" not in fresh
    assert "Все заявки укладываются в срок" in fresh
    assert "Просроченных заявок нет. Хорошая смена." in fresh
    # Просроченных нет — открыта первая непустая вкладка, «Новые».
    assert 'data-home-tab="new" aria-pressed="true"' in fresh

    monkeypatch.setattr(home, "utc_now", lambda: utc_now() + timedelta(minutes=31))
    later = (await client.get(HOME)).text
    assert _metric(later, "late") == 2
    assert "home-kpi-warn" in later
    assert "2 просроченные заявки. Начните с них." in later
    assert "Дольше всех ждёт №305: 31 мин" in later
    assert [_tab_count(later, key) for key in ("late", "new", "progress", "all")] == [2, 2, 1, 3]
    assert 'data-home-tab="late" aria-pressed="true"' in later
    # Строки — старшие сверху; у каждой — вкладки, где она видна; закрытой нет.
    assert _rows(later) == [
        ("late new all", "убрать 305", "overdue", "Просрочена"),
        ("late new all", "полотенца", "overdue", "Просрочена"),
        ("progress all", "лампа", "in_progress", "В работе"),
    ]
    assert "вода" not in later
    # «Взять себе» — только у невзятых; ссылка строки — поиск в очереди по номеру.
    assert later.count("data-claim=") == 2
    assert f'href="{HOME}/requests?q=%23' in later


async def test_stalled_request_is_marked_but_not_counted_as_overdue(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Долго в работе» (spec 0028 §8) — янтарная метка строки, но в «Просрочено»
    не идёт: там только невзятые, как в очереди. У отеля без срока для
    невзятых (напоминания выключены) просрочки нет — ни карточки, ни вкладки."""
    await store_hotel_config(
        portal_hotel.tenant_id, reminder_after_minutes=None, stalled_after_minutes=60
    )
    await _make_request(portal_hotel.tenant_id, "полотенца")
    taken = await _make_request(portal_hotel.tenant_id, "кондиционер")
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.change_request_status(taken.id, requests_api.RequestStatus.IN_PROGRESS)
    await submit_login(client, portal_hotel.login)

    monkeypatch.setattr(home, "utc_now", lambda: utc_now() + timedelta(minutes=61))
    page = (await client.get(HOME)).text
    assert 'data-kpi="late"' not in page
    assert _tab_count(page, "late") is None
    assert "2 открытые заявки." in page  # итог без «в срок»: срока нет
    assert _rows(page) == [
        ("new all", "полотенца", "new", "Новая"),
        ("progress all", "кондиционер", "overdue", "Долго в работе"),
    ]


async def test_each_tab_shows_the_oldest_rows_and_links_the_rest(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Во вкладке — не больше `ATTENTION_ROWS` дольше всех ждущих, остальные —
    «Ещё N — в очереди»; пустая вкладка говорит фразой, а не пустотой."""
    await store_hotel_config(portal_hotel.tenant_id)
    for index in range(home.ATTENTION_ROWS + 2):
        await _make_request(portal_hotel.tenant_id, f"заявка {index}", room=str(100 + index))
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    rows = _rows(page)
    assert [summary for _, summary, _, _ in rows] == [
        f"заявка {index}" for index in range(home.ATTENTION_ROWS)
    ]
    assert 'data-tab-more="new"' in page
    assert "Ещё 2 — <a" in page
    assert "Заявок в работе нет" in page


async def test_claim_from_home_uses_the_queue_action(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """«Взять себе» — тот же `POST …/api/requests/{id}/claim` с теми же правами
    и CSRF-контрактом: после него строка — «В работе» с именем взявшего."""
    request = await _make_request(portal_hotel.tenant_id, "полотенца")
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    endpoint = re.search(r'data-claim-endpoint="([^"]+)"', page)
    assert endpoint is not None
    assert f'data-claim="{request.id}"' in page

    claim_url = f"{endpoint.group(1)}/{request.id}/claim"
    assert (await client.post(claim_url, json={})).status_code == 403  # без Origin
    response = await client.post(claim_url, json={}, headers=SAME_ORIGIN)
    assert response.status_code == 200
    after = (await client.get(HOME)).text
    assert f'data-claim="{request.id}"' not in after
    assert _rows(after) == [("progress all", "полотенца", "in_progress", "В работе")]
    assert "Исполнитель: </span>Аружан Менеджер</span>" in after


async def test_today_numbers_follow_the_hotel_local_day(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Заезды и выезды сегодня — по полуночи Алматы, не UTC.

    Заселили сейчас, выезд — завтра 12:00 по отелю. За секунду до местной
    полуночи заселение ещё «сегодня», выезд — ещё нет; в полночь наоборот.
    """
    await store_hotel_config(portal_hotel.tenant_id)
    stay = await _check_in(portal_hotel.tenant_id, "101")
    await _check_in(portal_hotel.tenant_id, "102")
    midnight = _local_midnight_after(stay.check_in_at)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    monkeypatch.setattr(home, "utc_now", lambda: midnight - timedelta(seconds=1))
    before = (await client.get(HOME)).text
    assert _metric(before, "today") == 2
    assert "Выездов сегодня: 0" in before

    monkeypatch.setattr(home, "utc_now", lambda: midnight)
    after = (await client.get(HOME)).text
    assert _metric(after, "today") == 0
    assert "Выездов сегодня: 2" in after

    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_out(stay.id)
    checked_out = (await client.get(HOME)).text
    assert "Выездов сегодня: 1" in checked_out  # выехавший уже не «выезжает»


async def test_greeting_and_date_use_the_hotel_timezone(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """19.10 23:30 UTC — в Алматы (UTC+5) уже 20.10, 4:30 утра: «Доброй ночи»
    и вторник, а не «Добрый вечер» и понедельник по UTC."""
    await store_hotel_config(portal_hotel.tenant_id)
    monkeypatch.setattr(home, "utc_now", lambda: datetime(2026, 10, 19, 23, 30, tzinfo=UTC))
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert "Вторник, 20 октября" in response.text
    assert "Доброй ночи, Аружан" in response.text


async def test_home_opens_for_a_hotel_without_config(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """До онбординга конфига нет: главная открывается, приветствие — без времени
    суток и без даты (часов отеля мы не знаем), просрочки нет по определению —
    ни карточки, ни вкладки, ни «всё в срок»."""
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert response.status_code == 200
    assert "Здравствуйте, Аружан" in response.text
    assert "home-date" not in response.text
    assert _metric(response.text, "open") == 1
    assert 'data-kpi="late"' not in response.text
    assert "укладываются в срок" not in response.text
    assert "1 открытая заявка." in response.text
    assert _metric(response.text, "today") == 0
    assert "data-escalation-notice" not in response.text  # бот без конфига не работает целиком


async def _update_config(tenant_id: uuid.UUID, **update: object) -> None:
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        await store_tenant_config(session, tenant_id, config.model_copy(update=update))


@pytest.mark.parametrize(
    ("update", "shown"),
    [
        ({}, True),
        ({"escalation_category_key": "reception"}, False),
        ({"escalation_telegram_only": True}, False),
    ],
    ids=["no-choice", "service", "telegram-only"],
)
async def test_escalation_notice_tells_the_manager_about_a_hotel_without_choice(
    client: AsyncClient, portal_hotel: PortalHotel, update: dict[str, object], shown: bool
) -> None:
    """#786: служба эскалаций не выбрана и «только Telegram» не выбрано явно —
    гость, которому нужен человек, виден только в Telegram. Менеджер видит
    плашку; служба или явный выбор её снимают."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _update_config(portal_hotel.tenant_id, **update)
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    assert ("data-escalation-notice" in page) is shown
    assert (home.ESCALATION_NOTICE_TITLE in page) is shown


@pytest.mark.parametrize(
    ("role", "services"),
    [(StaffRole.STAFF, RECEPTION), (StaffRole.SENIOR, ("housekeeping",))],
    ids=["reception", "senior"],
)
async def test_escalation_notice_is_for_the_manager_only(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole, services: tuple[str, ...]
) -> None:
    """Чинит дыру администратор платформы, узнаёт о ней менеджер; остальным
    сотрудникам плашка — шум без действия."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _login_as(client, portal_hotel, role, services)
    page = (await client.get(HOME)).text
    assert 'data-kpi="open"' in page
    assert "data-escalation-notice" not in page


async def test_handover_panel_only_with_the_cash_section(
    client: AsyncClient, portal_hotel: PortalHotel, handover_enabled: None
) -> None:
    """«К передаче смены» — у кого открыт раздел кассы (ресепшен и менеджер):
    открытые, из них просрочено, без исполнителя."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(portal_hotel.tenant_id, "полотенца")
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    assert "К передаче смены" in page
    assert re.search(r"Без исполнителя</span><strong>1</strong>", page)
    assert f'href="{HOME}/handover"' in page

    client.cookies.clear()
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("housekeeping",))
    assert "К передаче смены" not in (await client.get(HOME)).text


async def test_home_keeps_the_signal_and_its_assets(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Сигнал кабинета звучит и на главной (spec 0039 §3.1); стили и скрипт
    главной отдаются как статика кабинета, инлайн-стилей нет (CSP)."""
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert 'id="signal"' in response.text
    assert "/staff/static/signal.js" in response.text
    for asset in (r"home\.css", r"home\.js"):
        found = re.search(rf'"(/staff/static/{asset}\?v=\w+)"', response.text)
        assert found is not None, asset
        assert (await client.get(found.group(1))).status_code == 200
    assert "style=" not in response.text  # CSP: инлайн-стили не пропускаются


async def test_guest_raised_row_reads_guest_reminds(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Spec 0070 §5, §7: поднятая напоминаниями гостя — красной строкой «Гость
    напоминает», как просроченная; одно напоминание строку не меняет."""
    await store_hotel_config(portal_hotel.tenant_id)
    towels = await _make_request(portal_hotel.tenant_id, "полотенца")
    water = await _make_request(portal_hotel.tenant_id, "вода", room="401")
    with tenant_context(portal_hotel.tenant_id):
        for _ in range(2):
            await requests_api.record_guest_reminder(towels.id)
        await requests_api.record_guest_reminder(water.id)
    await submit_login(client, portal_hotel.login)

    rows = _rows((await client.get(HOME)).text)

    assert [(summary, tone, label) for _, summary, tone, label in rows] == [
        ("полотенца", "overdue", "Гость напоминает"),
        ("вода", "new", "Новая"),
    ]

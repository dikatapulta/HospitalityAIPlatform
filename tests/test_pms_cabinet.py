"""Выгрузка Opera на странице заселения (spec 0058 §2.5, §5 п. 6).

Три полосы, блок «Не сходится с Opera», строки карточек PMS- и ручного Stay;
у отеля с ручным заселением — ничего. Тест на верхнем уровне, как
`test_pms_sync.py`: Fake-адаптер живёт в `integrations/`, а тестам кабинета он
не виден (контракт 2 import-linter). F811 отключён на файл: параметр-фикстура
обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.modules.guests.models import PmsSyncState, Stay
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.ports.pms import pms_or_none, set_pms
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import opera
from hospitality.staff_portal.tests.conftest import (  # noqa: F401  (фикстуры для pytest)
    HOTEL_SLUG,
    PortalHotel,
    _fake_rate_limit,
    client,
    portal_hotel,
    submit_login,
)

CHECKIN_PAGE = f"/staff/{HOTEL_SLUG}/checkin"
ZONE = ZoneInfo("Asia/Almaty")
Kind = guests_api.PmsDiscrepancyKind


@pytest.fixture
def pms(client: AsyncClient) -> Iterator[FakePmsAdapter]:
    """Fake-адаптер в порту — после `client`: `create_app` кладёт свой адаптер."""
    previous = pms_or_none()
    adapter = FakePmsAdapter()
    set_pms(adapter)
    try:
        yield adapter
    finally:
        if previous is not None:
            set_pms(previous)


async def _configure(tenant_id: uuid.UUID, pms_source: str = "opera_report") -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "pms_source": pms_source,
                }
            ),
        )


@pytest.fixture
async def opera_hotel(portal_hotel: PortalHotel, client: AsyncClient) -> PortalHotel:
    """Отель с `pms_source: opera_report` (интервал 5 минут) и вошедший менеджер."""
    await _configure(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    return portal_hotel


async def _sync_state(tenant_id: uuid.UUID, *, held: int, items: list[dict[str, object]]) -> None:
    """Состояние сверки так, как его пишет конец прогона (`pms_sync._finish`)."""
    discrepancies = [
        guests_api.PmsDiscrepancy.model_validate(item).model_dump(mode="json") for item in items
    ]
    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add(
                PmsSyncState(tenant_id=tenant_id, held_closures=held, discrepancies=discrepancies)
            )


async def _manual_stay(tenant_id: uuid.UUID, room: str, *, checked_in_ago: timedelta) -> None:
    """Заселение из кабинета «тогда-то»: сдвиг `check_in_at` назад."""
    with tenant_context(tenant_id):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + timedelta(days=2))
        )
        async with session_scope() as session:
            stay = await session.get(Stay, result.stay.id)
            assert stay is not None
            stay.check_in_at = utc_now() - checked_in_ago


def _book(pms: FakePmsAdapter, tenant_id: uuid.UUID, room: str, ref: str) -> None:
    arrival = utc_now().astimezone(ZONE).date()
    pms.check_in_room(tenant_id, room, ref, arrival=arrival, departure=arrival + timedelta(days=2))


def test_stays_word_follows_the_number() -> None:
    assert [opera._stays_word(n) for n in (11, 21, 34, 112, 25)] == [
        "проживаний",
        "проживание",
        "проживания",
        "проживаний",
        "проживаний",
    ]


def test_strip_priority_and_stale_date() -> None:
    """Одна полоса из трёх: «не приходили» → «не обновлялись» → «пропало»; вчерашний — с датой."""
    now = datetime(2026, 11, 2, 9, 0, tzinfo=ZONE)

    def status(taken_at: datetime | None, *, stale: bool, held: int) -> guests_api.PmsStatus:
        return guests_api.PmsStatus(
            interval_minutes=5, taken_at=taken_at, stale=stale, held_closures=held, discrepancies=[]
        )

    yesterday = datetime(2026, 11, 1, 14, 5, tzinfo=ZONE).astimezone(UTC)
    assert "ещё не приходили" in str(opera.strip_text(status(None, stale=False, held=3), ZONE, now))
    assert "не обновлялись с 01.11 14:05." in str(
        opera.strip_text(status(yesterday, stale=True, held=34), ZONE, now)
    )
    fresh = now.astimezone(UTC)
    assert "пропало сразу 34 проживания" in str(
        opera.strip_text(status(fresh, stale=False, held=34), ZONE, now)
    )
    assert opera.strip_text(status(fresh, stale=False, held=0), ZONE, now) is None


async def test_no_snapshot_yet_strip(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    page = (await client.get(CHECKIN_PAGE)).text
    assert "Данные из Opera ещё не приходили. Заселяйте гостей здесь, как раньше." in page


async def test_stale_snapshot_strip(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    """Старше трёх интервалов (5 мин × 3) — полоса с временем последнего снимка."""
    taken_at = utc_now() - timedelta(minutes=20)
    pms.publish(opera_hotel.tenant_id, taken_at=taken_at)

    page = (await client.get(CHECKIN_PAGE)).text

    assert "Данные из Opera не обновлялись с " in page
    assert taken_at.astimezone(ZONE).strftime("%H:%M") in page
    assert "Новые заселения сюда сами не попадут — заселяйте гостей здесь." in page


async def test_held_closures_strip_and_discrepancy_block(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    pms.publish(opera_hotel.tenant_id)
    await _sync_state(
        opera_hotel.tenant_id,
        held=34,
        items=[
            {
                "kind": Kind.OCCUPIED_BY_PMS,
                "room_number": "305",
                "booking_ref": "123456",
                "other_booking_ref": "654321",
            },
            {
                "kind": Kind.OCCUPIED_BY_MANUAL,
                "room_number": "306",
                "booking_ref": "223456",
                "arrival": date(2026, 10, 30),
                "manual_check_in": date(2026, 10, 28),
            },
            {
                "kind": Kind.MOVE_BLOCKED,
                "room_number": "307",
                "booking_ref": "323456",
                "target_room": "410",
            },
        ],
    )

    page = (await client.get(CHECKIN_PAGE)).text

    assert (
        "Из последней выгрузки Opera пропало сразу 34 проживания — похоже, файл неполный." in page
    )
    assert "Не сходится с Opera — 3" in page
    assert "Система тут ничего не меняла — решите вручную." in page
    assert (
        "Комната 305: в Opera заселена бронь 123456, а здесь — другое проживание (бронь 654321)."
        in page
    )
    assert (
        "Комната 306: в Opera заселена бронь 223456 с 30.10, а здесь — гость, заселённый "
        "вручную 28.10. Если он уехал — оформите выезд, бронь заселится сама." in page
    )
    assert "Бронь 323456 в Opera переехала в 410, но 410 здесь занята." in page
    assert f'href="{CHECKIN_PAGE}?room=307"' in page


async def test_discrepancy_from_real_sync_and_manual_card_line(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    """Сверка по Fake: ручной Stay другого гостя — строка блока; его карточка — «Нет в выгрузке»."""
    tenant_id = opera_hotel.tenant_id
    await _manual_stay(tenant_id, "305", checked_in_ago=timedelta(days=3))
    _book(pms, tenant_id, "305", "123456")
    pms.publish(tenant_id)
    await guests_api.sync_stays_from_pms()

    page = (await client.get(CHECKIN_PAGE)).text
    assert "Не сходится с Opera — 1" in page
    assert "Комната 305: в Opera заселена бронь 123456 с " in page
    assert "Данные из Opera" not in page  # снимок свежий, выезды не придержаны

    card = (await client.get(CHECKIN_PAGE, params={"room": "305"})).text
    assert "Нет в выгрузке Opera. Проверьте номер комнаты" in card


async def test_pms_stay_card_says_edit_in_opera(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    _book(pms, opera_hotel.tenant_id, "410", "777777")
    pms.publish(opera_hotel.tenant_id)
    await guests_api.sync_stays_from_pms()

    card = (await client.get(CHECKIN_PAGE, params={"room": "410"})).text

    assert (
        "Из Opera. Комнату и даты меняйте в Opera — правка здесь продержится до следующей "
        "выгрузки, около 5 минут." in card
    )
    assert "Нет в выгрузке Opera" not in card


async def test_fresh_manual_stay_card_has_no_opera_line(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    """Заселили 5 минут назад — Opera ещё не успела: строки нет."""
    await _manual_stay(opera_hotel.tenant_id, "305", checked_in_ago=timedelta(minutes=5))
    pms.publish(opera_hotel.tenant_id)

    card = (await client.get(CHECKIN_PAGE, params={"room": "305"})).text

    assert "Комната 305" in card
    assert "Opera" not in card


async def test_manual_hotel_sees_nothing_about_opera(
    client: AsyncClient, portal_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    """`pms_source: manual` — ни полосы, ни блока, ни строк карточки, даже со старым состоянием."""
    await _configure(portal_hotel.tenant_id, pms_source="manual")
    await submit_login(client, portal_hotel.login)
    await _manual_stay(portal_hotel.tenant_id, "305", checked_in_ago=timedelta(days=3))
    pms.publish(portal_hotel.tenant_id)
    await _sync_state(
        portal_hotel.tenant_id,
        held=34,
        items=[
            {
                "kind": Kind.MOVE_BLOCKED,
                "room_number": "305",
                "booking_ref": "1",
                "target_room": "2",
            }
        ],
    )

    page = (await client.get(CHECKIN_PAGE)).text
    card = (await client.get(CHECKIN_PAGE, params={"room": "305"})).text

    assert "Opera" not in page
    assert "Opera" not in card

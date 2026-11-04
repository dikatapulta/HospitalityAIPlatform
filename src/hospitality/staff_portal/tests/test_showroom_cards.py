"""Запрос из 3D-витрины в кабинете (spec 0069 §2.4, §5, §7 п. 6; PR (в), #805).

Карточка в очереди службы продаж: заголовок «Групповой запрос · компания · гости ·
даты», контакт, номера и пожелания — из модуля `showroom`, подсказка «Ответьте по
телефону или почте…», общие «Взять» и «Готово». Чужая служба карточку не видит.
Сигнал помечает вид запроса — уведомление «Групповой запрос #N» / «Запрос на
бронирование #N». Сводка дня называет источник «из 3D-витрины».
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.tests.request_fixtures import (  # noqa: F401 (фикстура scene)
    booking,
    configure,
    group,
    scene,
    showroom_config,
)
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import push_texts, showroom_cards, summary
from hospitality.staff_portal.signal import UnclaimedRequestView
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from hospitality.staff_portal.tests.test_bookings import _card, _login

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
API = f"/staff/{HOTEL_SLUG}/api"


async def _stand(portal_hotel: PortalHotel) -> None:
    with tenant_context(portal_hotel.tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            ("sales", "Продажи"),
            ("reservations", "Бронирование"),
            ("housekeeping", "Уборка"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
    await configure(portal_hotel.tenant_id, showroom_config())


async def _send(
    portal_hotel: PortalHotel, data: showroom_api.ShowroomRequestCreate
) -> showroom_api.ShowroomRequestPlaced:
    with tenant_context(portal_hotel.tenant_id):
        return await showroom_api.create_request(data, consent_version="2026-11-04-v1")


async def test_sales_sees_card_with_contacts_and_hint(
    portal_hotel: PortalHotel, client: AsyncClient, scene: object
) -> None:
    await _stand(portal_hotel)
    placed = await _send(portal_hotel, group())
    await _login(client, portal_hotel, ("sales",), "Динара")

    page = (await client.get(QUEUE)).text
    card = _card(page, placed.request.request_id)

    assert "Групповой запрос · Silk Road Travel · 32 гост. · " in card
    assert 'data-panel-title="Групповой запрос #1"' in card
    assert "Контакт: Anna Lee, +7 701 123 45 67, anna@example.com" in card
    assert "Номера: 1901, 1903" in card
    assert "Пожелания: Breakfast at 7" in card
    assert showroom_cards.ANSWER_HINT in card
    assert 'data-action="claim"' in card
    # Посетитель чата не видит: подпись отмены не обещает ему причину.
    assert "гость прочитает её в чате" not in card


async def test_other_service_does_not_see_the_request(
    portal_hotel: PortalHotel, client: AsyncClient, scene: object
) -> None:
    await _stand(portal_hotel)
    placed = await _send(portal_hotel, group())
    await _login(client, portal_hotel, ("housekeeping",), "Гульнара")

    page = (await client.get(QUEUE)).text

    assert str(placed.request.request_id) not in page
    assert "Anna Lee" not in page


async def test_signal_marks_kind_for_notification_title(
    portal_hotel: PortalHotel, client: AsyncClient, scene: object
) -> None:
    await _stand(portal_hotel)
    sales = await _send(portal_hotel, group())
    guest = await _send(portal_hotel, booking())
    await _login(client, portal_hotel, (RECEPTION_SERVICE_KEY,), "Айжан")

    unclaimed = (await client.get(f"{API}/requests/signal")).json()["unclaimed"]

    kinds = {item["id"]: item["showroom_kind"] for item in unclaimed}
    assert kinds == {
        str(sales.request.request_id): "group",
        str(guest.request.request_id): "booking",
    }
    titles = {
        push_texts.new_request(UnclaimedRequestView.model_validate(item), "/x").title
        for item in unclaimed
    }
    assert titles == {"Групповой запрос #1", "Запрос на бронирование #2"}
    assert all("Anna" not in str(item) and "Ержан" not in str(item) for item in unclaimed)


def test_booking_card_title_names_wish() -> None:
    read = showroom_api.StaffShowroomRequestRead(
        request_id=uuid.uuid4(),
        kind=showroom_api.ShowroomRequestKind.BOOKING,
        company=None,
        contact_name=None,
        phone=None,
        email=None,
        guests_count=2,
        check_in=date(2027, 3, 12),
        check_out=date(2027, 3, 15),
        room_numbers=[],
        wish_view_names=None,
        wish_type_names=None,
        wishes=None,
        language="ru",
        anonymized=True,
    )
    request = _request(daily_number=13)

    fields = showroom_cards.showroom_card(read, request)

    assert fields["title"] == "Запрос на бронирование · 12–15 марта · 2 гост."
    assert fields["panel_title"] == "Запрос на бронирование #13"
    assert fields["lines"] == [showroom_cards.ANONYMIZED_LINE]


def test_day_summary_names_showroom_source() -> None:
    origin = dict.fromkeys(requests_api.ServiceRequestOrigin, 0)
    origin[requests_api.ServiceRequestOrigin.GUEST_CHAT] = 3
    origin[requests_api.ServiceRequestOrigin.SHOWROOM_FORM] = 2
    day = requests_api.RequestsDaySummary(
        service_day=date(2026, 11, 5),
        day_start=utc_now(),
        day_end=utc_now() + timedelta(days=1),
        created_total=5,
        created_by_origin=origin,
        closed_total=0,
        closed_done=0,
        closed_cancelled=0,
        by_service=[],
        claim_median_seconds=None,
        overdue=None,
        open_now=5,
    )

    tiles = summary._tiles(day, None)

    assert tiles[0]["note"] == "через бота 3 · вручную 0 · из 3D-витрины 2"


def _request(*, daily_number: int) -> requests_api.ServiceRequestRead:
    now: datetime = utc_now()
    return requests_api.ServiceRequestRead.model_construct(
        id=uuid.uuid4(),
        daily_number=daily_number,
        status=requests_api.RequestStatus.NEW,
        created_at=now,
    )

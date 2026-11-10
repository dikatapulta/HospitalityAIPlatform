"""Оценка выполненной заявки в веб-чате (spec 0076 §5, §10 PR 2).

Виджет — под итогом `done`, под отменой его нет; без сессии — 401; Stay — из сессии, а не
из тела; комментарий пустой или длиннее 500 — 422; оценки выключены — 404 и виджета нет;
карточка не создалась — `passed_to_staff=False`, и страница не говорит «передадим».
Итог заявки пишется в диалог руками: подписчик `notify_guest_on_request_closed` в тестах
канала отключён (`_isolated_event_subscribers`), его запись `request_id` проверяет PR 1.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common.store import ensure_conversation, record_outbound_message
from hospitality.channels.web import page
from hospitality.channels.web.feedback import widget
from hospitality.channels.web.router import SESSION_COOKIE
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, RECEPTION_PHONE, ROOM, WebHotel
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import User
from hospitality.platform.staff_actions import StaffActor
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

BASE = f"/g/{HOTEL_SLUG}/{ROOM}"
Status = requests_api.RequestStatus


@dataclass(frozen=True)
class Stand:
    client: AsyncClient
    hotel: WebHotel
    staff: StaffActor


async def _configure(
    hotel: WebHotel, *, enabled: bool = True, escalation: str = "reception"
) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            hotel.tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=RECEPTION_PHONE,
                escalation_category_key=escalation,
                guest_feedback_enabled=enabled,
            ),
        )


@pytest.fixture
async def stand(web_hotel: WebHotel) -> AsyncIterator[Stand]:
    with tenant_context(web_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
    await _configure(web_hotel)
    async with platform_session_scope() as session:
        user = User(display_name="Айгерим")
        session.add(user)
        await session.flush()
        staff = StaffActor(user_id=user.id, display_name="Айгерим")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(f"{BASE}/session", json={"code": web_hotel.access_code})
        assert response.status_code == 200, response.text
        yield Stand(client=client, hotel=web_hotel, staff=staff)


async def _closed_request(
    stand: Stand, status: Status = Status.DONE, *, stay_id: uuid.UUID | None = None
) -> uuid.UUID:
    """Заявка гостя, закрытая сотрудником, и её итог в веб-диалоге сессии."""
    with tenant_context(stand.hotel.tenant_id):
        [category] = [c for c in await requests_api.list_categories() if c.key == "housekeeping"]
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="уборка номера",
                room_number=ROOM,
            ),
            stay_id=stay_id or stand.hotel.stay_id,
        )
        await requests_api.change_request_status(
            request.id, Status.IN_PROGRESS, acting_user=stand.staff
        )
        await requests_api.change_request_status(request.id, status, acting_user=stand.staff)
        token = stand.client.cookies.get(SESSION_COOKIE, path=f"/g/{HOTEL_SLUG}")
        session = await guests_api.resolve_session(token or "")
        assert session is not None
        conversation_id = await ensure_conversation(
            "web", str(session.guest_identity_id), guest_identity_id=session.guest_identity_id
        )
        await record_outbound_message(
            conversation_id,
            "Ваша заявка «уборка номера» выполнена.",
            "test",
            external_message_id=None,
            idempotency_key=f"test:closed:{request.id}",
            request_id=request.id,
        )
    return request.id


async def _feedback_by_message(stand: Stand) -> list[dict[str, object] | None]:
    response = await stand.client.get(f"{BASE}/messages")
    assert response.status_code == 200, response.text
    return [message["feedback"] for message in response.json()["messages"]]


async def _feedback_cards(stand: Stand) -> list[requests_api.ServiceRequestRead]:
    with tenant_context(stand.hotel.tenant_id):
        listing = await requests_api.list_requests(limit=100, offset=0)
    return [r for r in listing.items if r.origin is requests_api.ServiceRequestOrigin.FEEDBACK]


async def test_buttons_stand_under_done_and_not_under_cancel(stand: Stand) -> None:
    done = await _closed_request(stand)
    await _closed_request(stand, Status.CANCELLED)

    assert await _feedback_by_message(stand) == [
        {"request_id": str(done), "rating": None, "commented": False, "passed_to_staff": False},
        None,
    ]


async def test_thumbs_down_then_comment_reaches_the_card(stand: Stand) -> None:
    request_id = str(await _closed_request(stand))

    rated = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"}
    )
    again = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"}
    )
    other = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "up"}
    )
    commented = await stand.client.post(
        f"{BASE}/feedback/comment", json={"request_id": request_id, "comment": "  долго ждал  "}
    )
    second = await stand.client.post(
        f"{BASE}/feedback/comment", json={"request_id": request_id, "comment": "ещё"}
    )

    assert rated.status_code == 200, rated.text
    assert rated.json() == {
        "request_id": request_id,
        "rating": "down",
        "commented": False,
        "passed_to_staff": True,
    }
    assert again.json() == rated.json()  # двойное касание — тот же ответ (P-8)
    assert (other.status_code, other.json()["error"]["code"]) == (409, "ERR-FEEDBACK-003")
    assert commented.json()["passed_to_staff"] is True
    assert (second.status_code, second.json()["error"]["code"]) == (409, "ERR-FEEDBACK-004")
    [card] = await _feedback_cards(stand)
    assert "Гость написал: «долго ждал»" in (card.details or "")
    # Перезагрузка страницы показывает то же, что после касания.
    assert await _feedback_by_message(stand) == [
        {"request_id": request_id, "rating": "down", "commented": True, "passed_to_staff": True}
    ]


async def test_thumbs_up_is_kept_without_a_card(stand: Stand) -> None:
    request_id = str(await _closed_request(stand))

    rated = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "up"}
    )

    assert rated.json()["passed_to_staff"] is False
    assert await _feedback_cards(stand) == []
    assert (await _feedback_by_message(stand))[0] == {
        "request_id": request_id,
        "rating": "up",
        "commented": False,
        "passed_to_staff": False,
    }


async def test_without_a_session_it_is_401(stand: Stand) -> None:
    request_id = str(await _closed_request(stand))
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as stranger:
        rated = await stranger.post(
            f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"}
        )
        commented = await stranger.post(
            f"{BASE}/feedback/comment", json={"request_id": request_id, "comment": "плохо"}
        )

    assert (rated.status_code, rated.json()["error"]["code"]) == (401, "ERR-WEB-002")
    assert commented.status_code == 401
    assert await _feedback_cards(stand) == []


async def test_stay_comes_from_the_session_not_from_the_body(stand: Stand) -> None:
    """Чужое проживание в теле ничего не даёт: оценить можно только заявку своего Stay."""
    with tenant_context(stand.hotel.tenant_id):
        neighbour = await guests_api.check_in(
            guests_api.StayCheckIn(room_number="102", check_out_at=utc_now() + timedelta(days=1))
        )
    foreign = str(await _closed_request(stand, stay_id=neighbour.stay.id))

    rated = await stand.client.post(
        f"{BASE}/feedback",
        json={"request_id": foreign, "rating": "down", "stay_id": str(neighbour.stay.id)},
    )

    assert (rated.status_code, rated.json()["error"]["code"]) == (404, "ERR-FEEDBACK-002")
    assert await _feedback_by_message(stand) == [None]  # виджета под чужой заявкой нет
    assert await _feedback_cards(stand) == []


@pytest.mark.parametrize("comment", ["", "   ", "я" * 501])
async def test_empty_or_too_long_comment_is_422(stand: Stand, comment: str) -> None:
    request_id = str(await _closed_request(stand))
    await stand.client.post(f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"})

    response = await stand.client.post(
        f"{BASE}/feedback/comment", json={"request_id": request_id, "comment": comment}
    )

    assert (response.status_code, response.json()["error"]["code"]) == (422, "ERR-PLATFORM-002")


async def test_comment_of_500_characters_is_accepted(stand: Stand) -> None:
    request_id = str(await _closed_request(stand))
    await stand.client.post(f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"})

    response = await stand.client.post(
        f"{BASE}/feedback/comment", json={"request_id": request_id, "comment": "я" * 500}
    )

    assert response.status_code == 200, response.text


async def test_switched_off_feedback_is_404_and_has_no_widget(stand: Stand) -> None:
    request_id = str(await _closed_request(stand))
    await _configure(stand.hotel, enabled=False)

    rated = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"}
    )

    assert (rated.status_code, rated.json()["error"]["code"]) == (404, "ERR-FEEDBACK-001")
    assert await _feedback_by_message(stand) == [None]


async def test_failed_card_does_not_promise_to_pass_it_on(stand: Stand) -> None:
    """Службы эскалаций нет: оценка есть, карточки нет — ни в ответе, ни после
    перезагрузки страница не скажет гостю «передадим» (запись «потом» ревью PR #914)."""
    request_id = str(await _closed_request(stand))
    await _configure(stand.hotel, escalation="front-desk")

    rated = await stand.client.post(
        f"{BASE}/feedback", json={"request_id": request_id, "rating": "down"}
    )

    assert rated.json()["passed_to_staff"] is False
    assert (await _feedback_by_message(stand))[0] == {
        "request_id": request_id,
        "rating": "down",
        "commented": False,
        "passed_to_staff": False,
    }


async def test_widget_script_is_its_own_cached_file(stand: Stand) -> None:
    """Виджет — отдельным файлом (вес страницы, #746): адрес с версией в странице, кэш
    навсегда, надписи трёх языков и предел комментария — внутри."""
    response = await stand.client.get(widget.URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "immutable" in response.headers["cache-control"]
    assert "Бәрі қалай өтті?" in response.text and "How did it go?" in response.text
    assert "maxLength=500" in response.text.replace(" ", "")
    assert widget.URL in page.render()
    assert "Как всё прошло?" not in page.render()


def test_widget_styles_use_only_tokens_of_the_chat_page() -> None:
    """Свои токены файл не везёт: каждая переменная стилей виджета — в стилях страницы."""
    chat_css = page.render()
    for token in set(re.findall(r"var\((--[\w-]+)", widget.CSS)):
        assert f"{token}:" in chat_css, token

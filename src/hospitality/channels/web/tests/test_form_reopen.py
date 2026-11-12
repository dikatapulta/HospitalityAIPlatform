"""Заявку формы вернули в работу (#998, spec 0080 §4): поправка гостю и новое «выполнено».

Заказ в номер, бронь столика, запрос АВР: «Доставлено» / ответ / «Готово», затем
возврат — поправка на языке формы, затем новое закрытие — снова шаг закрытия.
Опоздавшее закрытие (вернули раньше, чем воркер доставил шаг) молчит, а поправлять
тогда нечего. «Готово» АВР — одно на проживание: возврат дубля, пока другой запрос
выполнен, поправки не даёт. Повтор доставки второго сообщения не даёт (P-8).

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import time

import pytest
from httpx import AsyncClient

from hospitality.channels.web.documents import messages as act_messages
from hospitality.channels.web.menu import messages as order_messages
from hospitality.channels.web.table import messages as booking_messages
from hospitality.channels.web.tests.conftest import WebHotel
from hospitality.channels.web.tests.test_documents import (
    CHAT,
    FORM,
    NO_FILES,
    _act_requests,
    _form,
    _sign_in,
    accounting,  # noqa: F401  (фикстура для pytest)
    documents_switch,  # noqa: F401  (фикстура для pytest)
)
from hospitality.channels.web.tests.test_room_service import (
    BASE,
    Menu,
    _enter,
    _order_body,
    _outbound,
    client,  # noqa: F401  (фикстура для pytest)
    menu,  # noqa: F401  (фикстура для pytest)
)
from hospitality.channels.web.tests.test_table_booking import _body, table_hotel  # noqa: F401
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope
from hospitality.shared.events import deliver_pending_events, subscribe
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)

Status = requests_api.RequestStatus


async def _staff() -> requests_api.ActingUser:
    async with platform_session_scope() as session:
        user = User(display_name="Aigerim")
        session.add(user)
        await session.flush()
        return requests_api.ActingUser(user_id=user.id, display_name="Aigerim")


async def _deliver() -> None:
    while await deliver_pending_events():
        pass


async def test_order_delivered_by_mistake_is_corrected_then_delivered(
    client: AsyncClient, menu: Menu
) -> None:
    order_messages.register()
    waiter = await _staff()
    await _enter(client, menu.hotel.access_code)
    first = (await client.post(f"{BASE}/orders", json=_order_body(menu.lagman, 3740_00))).json()
    late = (
        await client.post(f"{BASE}/orders", json=_order_body(menu.tea, 990_00, key="key-0002"))
    ).json()
    first_id, late_id = uuid.UUID(first["request_id"]), uuid.UUID(late["request_id"])
    with tenant_context(menu.hotel.tenant_id):
        for request_id in (first_id, late_id):
            await requests_api.change_request_status(
                request_id, Status.IN_PROGRESS, acting_user=waiter
            )
            await requests_api.mark_request_paid(request_id, acting_user=waiter)
        await requests_api.change_request_status(first_id, Status.DONE, acting_user=waiter)
        await _deliver()
        await requests_api.reopen_request(first_id)
        await _deliver()
        await requests_api.change_request_status(first_id, Status.DONE, acting_user=waiter)
        # Второй заказ вернули раньше, чем воркер доставил «Доставлено».
        await requests_api.change_request_status(late_id, Status.DONE, acting_user=waiter)
        await requests_api.reopen_request(late_id)
        await _deliver()
        for _ in range(2):  # повтор доставки — второго сообщения нет (P-8)
            await order_messages.notify_order_step(first_id, order_messages.OrderStep.REOPENED)
            await order_messages.notify_order_step(first_id, order_messages.OrderStep.DELIVERED)

    steps = [text for text in await _outbound(client) if "#1" in text and "sent to" not in text]
    late_steps = [text for text in await _outbound(client) if "#2" in text]
    assert steps == [
        "A staff member is on the way for payment for order #1 — 3,740 ₸, cash or card.",
        "Payment received — order #1 is accepted and we're cooking. We'll bring it to room 101.",
        "Order #1 has been delivered. Enjoy your meal!",
        "Sorry, we were too quick: order #1 has not been delivered yet. We're already on it.",
        "Order #1 has been delivered. Enjoy your meal!",
    ]
    assert not any("delivered" in text for text in late_steps)


async def test_booking_answer_sent_by_mistake_is_corrected_and_answered_again(
    client: AsyncClient, table_hotel: WebHotel
) -> None:
    booking_messages.register()
    subscribe(requests_api.RequestStatusChanged, table_booking_api.withdraw_answer_on_reopen)
    staff = await _staff()
    await _enter(client, table_hotel.access_code)
    booking = (
        await client.post(f"{BASE}/table/bookings", json=_body(key="key-0001", language="ru"))
    ).json()
    request_id = uuid.UUID(booking["request_id"])
    answer = table_booking_api.BookingAnswer
    with tenant_context(table_hotel.tenant_id):
        await table_booking_api.answer_booking(request_id, answer.CONFIRMED, acting_user=staff)
        await _deliver()
        await requests_api.reopen_request(request_id)
        await _deliver()
        waiting = await table_booking_api.get_booking_by_request(request_id)
        await table_booking_api.answer_booking(
            request_id, answer.OTHER_TIME, time(20, 30), acting_user=staff
        )
        await _deliver()

    texts = await _outbound(client)
    assert waiting is not None and waiting.stage is table_booking_api.BookingStage.WAITING
    assert [text.split(":")[0] for text in texts] == [
        "Запрос отправлен в ресторан «Терраса»",
        "Столик забронирован",
        "Простите, мы поторопились",
        "В ресторане «Терраса» на 19",
    ]
    assert "отправлен по ошибке. Ресторан ещё проверяет места и ответит здесь." in texts[2]


async def test_act_ready_by_mistake_is_corrected_unless_a_duplicate_is_ready(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    act_messages.register()
    await _sign_in(client, web_hotel)
    sent = await client.post(
        FORM, data=_form(bill_to="ТОО Ромашка", language="en-GB"), files=NO_FILES
    )
    assert sent.status_code == 200, sent.text
    (first,) = await _act_requests(web_hotel)
    with tenant_context(web_hotel.tenant_id):
        await requests_api.change_request_status(first.request_id, Status.IN_PROGRESS)
        await requests_api.change_request_status(first.request_id, Status.DONE)
        await _deliver()
        await requests_api.reopen_request(first.request_id)
        await _deliver()
        await requests_api.change_request_status(first.request_id, Status.DONE)
        await _deliver()
    duplicate = await client.post(
        FORM, data=_form(bill_to="ТОО Ромашка", language="en-GB"), files=NO_FILES
    )
    assert duplicate.status_code == 200, duplicate.text
    second = next(row for row in await _act_requests(web_hotel) if row.id != first.id)
    with tenant_context(web_hotel.tenant_id):
        await requests_api.change_request_status(second.request_id, Status.IN_PROGRESS)
        await requests_api.change_request_status(second.request_id, Status.DONE)
        await _deliver()
        # Дубль вернули, а первый запрос выполнен — его «Готово» остаётся правдой.
        await requests_api.reopen_request(second.request_id)
        await requests_api.change_request_status(second.request_id, Status.DONE)
        await _deliver()

    response = await client.get(f"{CHAT}/messages")
    texts = [m["text"] for m in response.json()["messages"] if m["direction"] == "outbound"]
    ready = "📄 Accounting has prepared your closing documents."
    sorry = "📄 Sorry, we were too quick: your closing documents are not ready yet."
    steps = [text for text in texts if text.startswith((ready, sorry))]
    assert [text[: len(ready)] if text.startswith(ready) else sorry for text in steps] == [
        ready,
        sorry,
        ready,
    ]

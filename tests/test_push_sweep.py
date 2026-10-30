"""Рассылка пуша на телефоны персонала (spec 0050 §6, §11 PR 2; issue #477).

Проход `run_push_sweep` зовётся напрямую — как его зовёт задача воркера.
Служба пуша — Fake-адаптер (ADR-007) в порту: отправленное остаётся в `sent`
открытым текстом, исход адреса задаёт `set_outcome`. Тесты лежат в `tests/`,
а не в кабинете: кабинету `integrations/` не видны (контракт 2 import-linter).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from structlog.testing import capture_logs

from hospitality.integrations.web_push import FakeWebPushAdapter
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_push, staff_push_journal
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import deactivate_user
from hospitality.platform.staff_push import StaffPushSubscription
from hospitality.platform.staff_superadmin import erase_member
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.ports.web_push import (
    PushDelivery,
    PushOutcome,
    PushTarget,
    b64url_encode,
    set_web_push,
    web_push_or_none,
)
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import push_sweep, push_texts
from hospitality.staff_portal.tests.conftest import store_hotel_config
from hospitality.staff_portal.tests.test_queue import _make_request
from tests.test_staff_auth import create_staff_user, unique_login
from tests.test_staff_superadmin import create_superadmin

FCM = "https://fcm.googleapis.com/fcm/send/"
RECEPTION = ("reception",)
HOUSEKEEPING = ("housekeeping",)
MAINTENANCE = ("maintenance",)


class _AuthFailingPush(FakeWebPushAdapter):
    """Служба пуша, которая не принимает подпись сервера (не тот ключ VAPID)."""

    async def send(self, target: PushTarget, payload: bytes, *, ttl_seconds: int) -> PushDelivery:
        return PushDelivery(PushOutcome.FAILED, 403)


@pytest.fixture
def fake_push() -> Iterator[FakeWebPushAdapter]:
    previous = web_push_or_none()
    adapter = FakeWebPushAdapter()
    set_web_push(adapter)
    push_sweep._failing_now.clear()
    yield adapter
    set_web_push(previous)
    push_sweep._failing_now.clear()


@pytest.fixture
def board_enabled() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _hotel(slug: str = "push-hotel") -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name="Push Hotel")
        session.add(tenant)
        await session.flush()
        return tenant.id


@pytest.fixture
async def hotel(canonical_database: None, fake_push: FakeWebPushAdapter) -> uuid.UUID:
    return await _hotel()


async def _member(
    tenant_id: uuid.UUID, services: tuple[str, ...], role: StaffRole = StaffRole.STAFF
) -> uuid.UUID:
    return await create_staff_user(
        unique_login(), tenant_id=tenant_id, role=role, service_keys=services
    )


async def _phone(tenant_id: uuid.UUID, user_id: uuid.UUID, name: str) -> str:
    """Подписка телефона — как её пишет `POST …/api/push/subscriptions`."""
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    endpoint = FCM + name
    with tenant_context(tenant_id):
        await staff_push.save_subscription(
            user_id, endpoint, b64url_encode(public), b64url_encode(os.urandom(16))
        )
    return endpoint


async def _sweep() -> push_sweep.SweepStats:
    return await push_sweep.run_push_sweep()


def _titles(fake: FakeWebPushAdapter, endpoint: str) -> list[str]:
    return [json.loads(push.payload)["title"] for push in fake.sent if push.endpoint == endpoint]


async def _subscriptions(tenant_id: uuid.UUID) -> list[StaffPushSubscription]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(StaffPushSubscription)))


async def _journal(tenant_id: uuid.UUID) -> dict[uuid.UUID, frozenset[str]]:
    with tenant_context(tenant_id):
        return await staff_push_journal.load_announced()


async def test_new_request_calls_its_service_once(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    maid = await _member(hotel, HOUSEKEEPING)
    technician = await _member(hotel, MAINTENANCE)
    maid_phone = await _phone(hotel, maid, "maid")
    technician_phone = await _phone(hotel, technician, "technician")
    await _sweep()  # первое включение: ждать нечего — тишина

    request = await _make_request(hotel, "SECRET-GUEST-TEXT", room_number="305")
    await _sweep()
    await _sweep()  # повтор прохода — тишина

    assert _titles(fake_push, maid_phone) == [f"Новая заявка #{request.daily_number}"]
    assert _titles(fake_push, technician_phone) == []
    (sent,) = fake_push.sent
    message = json.loads(sent.payload)
    assert message["body"] == "Комната 305 · Уборка"
    assert message["url"] == "/staff/push-hotel/requests"
    assert message["tag"] == f"request-{request.id}-new"
    assert b"SECRET-GUEST-TEXT" not in sent.payload
    assert sent.ttl_seconds == push_sweep.PUSH_TTL_SECONDS


async def test_first_switch_on_calls_only_urgent(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    await _make_request(hotel, "обычная", room_number="301")
    urgent = await _make_request(hotel, "срочная", room_number="302", is_urgent=True)
    maid = await _member(hotel, HOUSEKEEPING)
    phone = await _phone(hotel, maid, "maid")

    await _sweep()
    await _sweep()

    assert _titles(fake_push, phone) == [f"🚨 Срочная заявка #{urgent.daily_number}"]
    later = await _make_request(hotel, "после включения", room_number="303")
    await _sweep()
    assert _titles(fake_push, phone)[-1] == f"Новая заявка #{later.daily_number}"


async def test_taken_request_leaves_the_journal(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    maid = await _member(hotel, HOUSEKEEPING)
    await _phone(hotel, maid, "maid")
    await _sweep()
    request = await _make_request(hotel)
    await _sweep()
    assert (await _journal(hotel))[maid] == {f"new:{request.id}:{request.category_id}"}

    with tenant_context(hotel):
        await requests_api.change_request_status(request.id, requests_api.RequestStatus.IN_PROGRESS)
    await _sweep()

    assert await _journal(hotel) == {}


async def test_overdue_then_raised_reach_service_and_reception(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Через срок — «ждёт уже» службе; через два — «Никто не взял» ресепшену."""
    await store_hotel_config(hotel, reminder_after_minutes=30)
    maid = await _member(hotel, HOUSEKEEPING)
    receptionist = await _member(hotel, RECEPTION)
    maid_phone = await _phone(hotel, maid, "maid")
    reception_phone = await _phone(hotel, receptionist, "reception")
    await _sweep()
    request = await _make_request(hotel)
    await _sweep()
    start = utc_now()

    monkeypatch.setattr(
        "hospitality.staff_portal.signal.utc_now", lambda: start + timedelta(minutes=31)
    )
    await _sweep()
    monkeypatch.setattr(
        "hospitality.staff_portal.signal.utc_now", lambda: start + timedelta(minutes=61)
    )
    await _sweep()
    await _sweep()

    number = request.daily_number
    maid_titles = _titles(fake_push, maid_phone)
    assert maid_titles[0] == f"Новая заявка #{number}"
    assert maid_titles[1].startswith(f"Заявка #{number} ждёт уже ")
    assert len(maid_titles) == 2
    assert _titles(fake_push, reception_phone) == [f"Никто не взял заявку #{number}"]


async def test_board_call_reaches_housekeeping(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter, board_enabled: None
) -> None:
    maid = await _member(hotel, HOUSEKEEPING)
    phone = await _phone(hotel, maid, "maid")
    await _sweep()

    with tenant_context(hotel):
        await housekeeping_api.report_checkout("704")
    await _sweep()
    await _sweep()

    (sent,) = fake_push.sent
    message = json.loads(sent.payload)
    assert (sent.endpoint, message["title"], message["body"]) == (
        phone,
        "704 — выезд",
        "Проверьте номер",
    )
    assert message["url"] == "/staff/push-hotel/turnovers"


@pytest.fixture
def floors_enabled() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_HOUSEKEEPING_FLOORS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@pytest.mark.usefixtures("floors_enabled")
async def test_floor_maids_of_one_service_hear_different_floors(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    """Раздача этажей — часть ключа группы (spec 0053 §4): у двух горничных одни
    службы, а 704 будит только телефон горничной 7-го этажа."""
    seventh = await _member(hotel, HOUSEKEEPING)
    eighth = await _member(hotel, HOUSEKEEPING)
    seventh_phone = await _phone(hotel, seventh, "seventh")
    eighth_phone = await _phone(hotel, eighth, "eighth")
    with tenant_context(hotel):
        await housekeeping_api.replace_floor_assignments(
            {seventh: frozenset({7}), eighth: frozenset({8})}
        )
    await _sweep()

    request = await _make_request(hotel, "полотенца", room_number="704")
    await _sweep()

    assert _titles(fake_push, seventh_phone) == [f"Новая заявка #{request.daily_number}"]
    assert _titles(fake_push, eighth_phone) == []


async def test_all_phones_failed_is_retried_one_delivered_is_not(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    maid = await _member(hotel, HOUSEKEEPING)
    first = await _phone(hotel, maid, "first")
    second = await _phone(hotel, maid, "second")
    await _sweep()
    fake_push.set_outcome(first, PushOutcome.FAILED)
    fake_push.set_outcome(second, PushOutcome.FAILED)
    request = await _make_request(hotel)

    with capture_logs() as logs:
        stats = await _sweep()
    assert (stats.delivered, stats.failed) == (0, 2)
    assert await _journal(hotel) == {}
    unavailable = [entry for entry in logs if entry["event"] == "staff_push.unavailable"]
    assert len(unavailable) == 1  # одна строка на проход, а не на отправку

    fake_push.set_outcome(first, PushOutcome.DELIVERED)
    await _sweep()
    await _sweep()

    assert _titles(fake_push, first) == [f"Новая заявка #{request.daily_number}"]
    assert len(await _subscriptions(hotel)) == 2  # не дошло — подписка остаётся


async def test_gone_phone_is_deleted_and_the_call_is_not_repeated(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    maid = await _member(hotel, HOUSEKEEPING)
    phone = await _phone(hotel, maid, "maid")
    await _sweep()
    fake_push.set_outcome(phone, PushOutcome.GONE)
    await _make_request(hotel)

    stats = await _sweep()

    assert stats.deleted == 1
    assert await _subscriptions(hotel) == []


async def test_auth_rejection_keeps_phones_and_alerts_once(
    canonical_database: None, fake_push: FakeWebPushAdapter
) -> None:
    """403 у службы пуша — не тот ключ VAPID: подписки не удаляются (Н-8 ревью
    PR #501), ERROR — один раз на эпизод, дальше WARNING."""
    hotel = await _hotel()
    maid = await _member(hotel, HOUSEKEEPING)
    await _phone(hotel, maid, "maid")
    await _sweep()
    set_web_push(_AuthFailingPush())
    await _make_request(hotel)

    with capture_logs() as logs:
        await _sweep()
        await _sweep()

    levels = [entry["log_level"] for entry in logs if entry["event"] == "staff_push.auth_rejected"]
    assert levels == ["error", "warning"]
    assert len(await _subscriptions(hotel)) == 1


async def test_deactivated_member_gets_nothing_and_loses_phones(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    manager = await _member(hotel, (), role=StaffRole.MANAGER)
    maid = await _member(hotel, HOUSEKEEPING)
    phone = await _phone(hotel, maid, "maid")
    await _sweep()
    await deactivate_user(maid, actor_user_id=manager)
    await _make_request(hotel)

    await _sweep()

    assert _titles(fake_push, phone) == []
    assert await _subscriptions(hotel) == []


async def test_erase_deletes_member_phones(hotel: uuid.UUID, fake_push: FakeWebPushAdapter) -> None:
    """Стирание не удаляет строку `users` — подписки удаляет само (Н-1 ревью PR #501)."""
    admin_id, _ = await create_superadmin(hotel, unique_login())
    maid = await _member(hotel, HOUSEKEEPING)
    await _phone(hotel, maid, "maid")

    await erase_member(maid, hotel, actor_user_id=admin_id)

    assert await _subscriptions(hotel) == []


async def test_stored_endpoint_the_check_now_refuses_is_dropped(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    """Строка, записанная до ужесточения проверки, — отказ и удаление, а не
    падение прохода (Н-2 ревью PR #501)."""
    maid = await _member(hotel, HOUSEKEEPING)
    phone = await _phone(hotel, maid, "maid")
    await _sweep()
    with tenant_context(hotel):
        async with session_scope() as session:
            row = await session.scalar(select(StaffPushSubscription))
            assert row is not None
            row.endpoint = "https://xn--a.push.apple.com/x"
    await _make_request(hotel)

    stats = await _sweep()

    assert (stats.tenants_failed, stats.deleted) == (0, 1)
    assert _titles(fake_push, phone) == []
    assert await _subscriptions(hotel) == []


async def test_other_hotel_phones_do_not_hear_this_hotel(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    other = await _hotel("push-other")
    here = await _phone(hotel, await _member(hotel, HOUSEKEEPING), "here")
    there = await _phone(other, await _member(other, HOUSEKEEPING), "there")
    await _sweep()

    await _make_request(hotel)
    await _sweep()

    assert len(_titles(fake_push, here)) == 1
    assert _titles(fake_push, there) == []


async def test_failing_hotel_does_not_stop_the_others(
    canonical_database: None, fake_push: FakeWebPushAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой у одного отеля — ERROR ERR-PUSH-005 о нём, остальным рассылка идёт.
    Сломанный заведён первым — проход доходит до него раньше живого."""
    broken = await _hotel("push-broken")
    hotel = await _hotel()
    phone = await _phone(hotel, await _member(hotel, HOUSEKEEPING), "here")
    await _phone(broken, await _member(broken, HOUSEKEEPING), "broken")
    await _sweep()
    real_sweep_tenant = push_sweep._sweep_tenant

    async def sweep_tenant(
        tenant_id: uuid.UUID, tenant_slug: str, sender: push_sweep._Sender
    ) -> None:
        if tenant_slug == "push-broken":
            raise RuntimeError("broken hotel")
        await real_sweep_tenant(tenant_id, tenant_slug, sender)

    monkeypatch.setattr(push_sweep, "_sweep_tenant", sweep_tenant)
    await _make_request(hotel)

    with capture_logs() as logs:
        stats = await _sweep()

    assert stats.tenants_failed == 1
    assert len(_titles(fake_push, phone)) == 1
    (failed,) = [entry for entry in logs if entry["event"] == "staff_push.sweep_failed"]
    assert (failed["log_level"], failed["tenant_id"]) == ("error", str(broken))


async def test_push_off_means_no_sweep(canonical_database: None) -> None:
    previous = web_push_or_none()
    set_web_push(None)
    try:
        assert await _sweep() == push_sweep.SweepStats()
    finally:
        set_web_push(previous)


def test_phone_texts_match_the_computer_notification() -> None:
    """Слова уведомления на телефоне — те же, что в `signal.js` (spec 0050 §7)."""
    script = Path("src/hospitality/staff_portal/static/signal.js").read_text(encoding="utf-8")
    source = Path(push_texts.__file__).read_text(encoding="utf-8")
    phrases = {
        "Новая заявка",
        "Новый заказ",
        "🚨 Срочная заявка",
        "Никто не взял заявку",
        "ждёт уже",
        "Комната ",
        " — выезд",
        " убран",
        " — замечание",
        "Проверьте номер",
        "Примите номер",
    }
    for phrase in phrases:
        assert phrase in source, phrase
        assert phrase in script, phrase
    tags = set(re.findall(r'"(request|turnover)-', script))
    assert tags == {"request", "turnover"}

"""Уведомления на телефон — подписка и проверка (spec 0050 §3–§5, PR 1 по #477).

Служба пуша — Fake-адаптер (ADR-007), который `create_app` кладёт в порт по
умолчанию (`STAFF_PUSH_BACKEND=fake`): сообщения остаются в `fake.sent`, исход
адреса задаётся `set_outcome`. Тест берёт его через порт, а не импортом —
кабинет не видит `integrations/` (контракт 2 import-linter). Ключи подписки —
настоящая пара P-256, как у браузера.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any, Protocol, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_push import MAX_SUBSCRIPTIONS_PER_MEMBER, StaffPushSubscription
from hospitality.shared.db import session_scope
from hospitality.shared.ports.web_push import (
    PushOutcome,
    b64url_encode,
    set_web_push,
    web_push_or_none,
)
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import create_staff_user, unique_login

API = f"/staff/{HOTEL_SLUG}/api/push"
HOME = f"/staff/{HOTEL_SLUG}"
SAME_ORIGIN = {"origin": "https://test"}
FCM = "https://fcm.googleapis.com/fcm/send/"


class _FakePush(Protocol):
    """То, что тесту нужно от Fake-адаптера сверх порта."""

    application_server_key: str
    sent: list[Any]

    def set_outcome(self, endpoint: str, outcome: PushOutcome) -> None: ...


@pytest.fixture(autouse=True)
def _restore_web_push() -> Iterator[None]:
    """Тест «пуш выключен» снимает адаптер с порта — следующим он нужен обратно."""
    previous = web_push_or_none()
    yield
    set_web_push(previous)


def _fake() -> _FakePush:
    adapter = web_push_or_none()
    assert adapter is not None and adapter.backend == "fake"
    return cast(_FakePush, adapter)


def _subscription(endpoint: str) -> dict[str, object]:
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": b64url_encode(public), "auth": b64url_encode(os.urandom(16))},
    }


async def _subscriptions(portal_hotel: PortalHotel) -> list[StaffPushSubscription]:
    with tenant_context(portal_hotel.tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(StaffPushSubscription).order_by(StaffPushSubscription.created_at)
            )
            return list(rows)


async def _login_member(client: AsyncClient, portal_hotel: PortalHotel) -> uuid.UUID:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=("housekeeping",),
        display_name="Айгуль",
    )
    await submit_login(client, staff_login)
    return user_id


async def _subscribe(client: AsyncClient, endpoint: str) -> int:
    response = await client.post(
        f"{API}/subscriptions", json=_subscription(endpoint), headers=SAME_ORIGIN
    )
    return response.status_code


async def test_subscription_is_stored_for_the_member(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    user_id = await _login_member(client, portal_hotel)
    assert await _subscribe(client, FCM + "phone-1") == 204

    (row,) = await _subscriptions(portal_hotel)
    assert (row.user_id, row.endpoint, row.tenant_id) == (
        user_id,
        FCM + "phone-1",
        portal_hotel.tenant_id,
    )


async def test_same_phone_under_another_login_moves_to_new_member(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "shared-phone")
    second_user = await _login_member(second_client, portal_hotel)
    assert await _subscribe(second_client, FCM + "shared-phone") == 204

    (row,) = await _subscriptions(portal_hotel)
    assert row.user_id == second_user


async def test_sixth_phone_evicts_the_oldest(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    for number in range(MAX_SUBSCRIPTIONS_PER_MEMBER + 1):
        await _subscribe(client, f"{FCM}phone-{number}")

    endpoints = {row.endpoint for row in await _subscriptions(portal_hotel)}
    assert len(endpoints) == MAX_SUBSCRIPTIONS_PER_MEMBER
    assert FCM + "phone-0" not in endpoints


@pytest.mark.parametrize(
    "endpoint", ["https://169.254.169.254/latest/meta-data", "http://fcm.googleapis.com/x"]
)
async def test_endpoint_outside_push_services_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel, endpoint: str
) -> None:
    await _login_member(client, portal_hotel)
    response = await client.post(
        f"{API}/subscriptions", json=_subscription(endpoint), headers=SAME_ORIGIN
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-PUSH-001"
    assert await _subscriptions(portal_hotel) == []


async def test_actions_require_same_origin_json(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    response = await client.post(f"{API}/subscriptions", json=_subscription(FCM + "x"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-009"


async def test_actions_require_session(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.post(
        f"{API}/subscriptions", json=_subscription(FCM + "x"), headers=SAME_ORIGIN
    )
    assert response.status_code == 401


async def test_test_push_reaches_this_phone_without_guest_text(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "phone")
    response = await client.post(
        f"{API}/test", json={"endpoint": FCM + "phone"}, headers=SAME_ORIGIN
    )

    assert response.json() == {"outcome": "delivered"}
    (sent,) = _fake().sent
    assert sent.endpoint == FCM + "phone" and sent.ttl_seconds == 60
    assert json.loads(sent.payload) == {
        "title": "Проверка уведомлений",
        "body": "Всё работает — так телефон позовёт о новой заявке",
        "url": HOME,
        "tag": "push-test",
    }
    (row,) = await _subscriptions(portal_hotel)
    assert row.last_delivered_at is not None


@pytest.mark.parametrize("outcome", [PushOutcome.GONE, PushOutcome.REJECTED])
async def test_dead_subscription_is_forgotten(
    client: AsyncClient, portal_hotel: PortalHotel, outcome: PushOutcome
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "phone")
    _fake().set_outcome(FCM + "phone", outcome)
    response = await client.post(
        f"{API}/test", json={"endpoint": FCM + "phone"}, headers=SAME_ORIGIN
    )

    assert response.json() == {"outcome": outcome.value}
    assert await _subscriptions(portal_hotel) == []


async def test_unavailable_push_service_keeps_subscription(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "phone")
    _fake().set_outcome(FCM + "phone", PushOutcome.FAILED)
    response = await client.post(
        f"{API}/test", json={"endpoint": FCM + "phone"}, headers=SAME_ORIGIN
    )

    assert response.json() == {"outcome": "failed"}
    assert len(await _subscriptions(portal_hotel)) == 1


async def test_test_push_only_to_own_phone(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "her-phone")
    await _login_member(second_client, portal_hotel)
    response = await second_client.post(
        f"{API}/test", json={"endpoint": FCM + "her-phone"}, headers=SAME_ORIGIN
    )

    assert response.json() == {"outcome": "missing"}
    assert _fake().sent == []


async def test_test_push_is_rate_limited(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Один инстанс на тест: счётчик обязан копиться между запросами.
    fake_limits = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_limits)
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "phone")
    body = {"endpoint": FCM + "phone"}
    await client.post(f"{API}/test", json=body, headers=SAME_ORIGIN)
    response = await client.post(f"{API}/test", json=body, headers=SAME_ORIGIN)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "ERR-PUSH-004"
    assert len(_fake().sent) == 1


async def test_logout_deletes_only_this_phone(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    await _subscribe(client, FCM + "phone")
    await _subscribe(client, FCM + "tablet")
    response = await client.post(
        f"{API}/subscriptions/delete", json={"endpoint": FCM + "phone"}, headers=SAME_ORIGIN
    )

    assert response.status_code == 204
    assert [row.endpoint for row in await _subscriptions(portal_hotel)] == [FCM + "tablet"]


async def test_pages_offer_push_with_server_key(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    page = (await client.get(HOME)).text

    assert f'data-key="{_fake().application_server_key}"' in page
    assert f'data-subscribe-url="{API}/subscriptions"' in page
    assert "/staff/static/push.js?v=" in page
    assert "Уведомления на телефон" in page
    assert 'rel="manifest" href="/staff/static/manifest.webmanifest?v=' in page


async def test_push_off_hides_button_and_closes_actions(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_member(client, portal_hotel)
    set_web_push(None)
    page = (await client.get(HOME)).text
    response = await client.post(
        f"{API}/subscriptions", json=_subscription(FCM + "x"), headers=SAME_ORIGIN
    )

    assert 'id="push"' not in page and "push.js" not in page
    assert response.status_code == 404


async def test_service_worker_covers_whole_cabinet(client: AsyncClient) -> None:
    response = await client.get("/staff/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert 'addEventListener("push"' in response.text


async def test_manifest_installs_cabinet_as_standalone_app(client: AsyncClient) -> None:
    response = await client.get("/staff/static/manifest.webmanifest")
    manifest = response.json()

    assert response.headers["content-type"].startswith("application/manifest+json")
    assert manifest["display"] == "standalone"
    assert (manifest["start_url"], manifest["scope"]) == ("/staff/", "/staff/")
    assert (manifest["short_name"], manifest["name"]) == ("Кабинет", "Кабинет персонала")
    for icon in manifest["icons"]:
        image = await client.get(icon["src"])
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"

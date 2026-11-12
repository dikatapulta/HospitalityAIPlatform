"""Фото гостя к заявке в веб-чате — маршруты и страница (spec 0081 §4, §10; issue #82)."""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from structlog.testing import capture_logs

from hospitality.app import create_app
from hospitality.channels.common import guest_photo
from hospitality.channels.common.models import Conversation
from hospitality.channels.common.store import record_request_origin
from hospitality.channels.web.photos import router as photos_router
from hospitality.channels.web.photos import widget
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import FilePurpose, store_upload
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (
    FakeRateLimitRedis,
    fake_file_storage,  # noqa: F401  (фикстура для pytest)
)
from tests.test_file_content import make_png

CHAT = f"/g/{HOTEL_SLUG}/{ROOM}"
PHOTOS = f"{CHAT}/photos"
PNG = make_png()


def _switch(**env: str) -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    for name, value in env.items():
        patcher.setenv(name, value)
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@pytest.fixture
def photos_on() -> Iterator[None]:
    """Фото включены: выключатель контура и заявки (без заявок фото некуда)."""
    yield from _switch(ENABLE_GUEST_REQUEST_PHOTOS="true", ENABLE_SERVICE_REQUESTS="true")


@pytest.fixture
async def client(
    web_hotel: WebHotel, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    del web_hotel  # нужен ради тенанта и заселённой комнаты
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


async def _sign_in(client: AsyncClient, web_hotel: WebHotel) -> None:
    response = await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
    assert response.status_code == 200, response.text


async def _send(
    client: AsyncClient, data: bytes = PNG, name: str = "tap.png", key: str | None = None
) -> Response:
    return await client.post(
        PHOTOS,
        data={"client_key": key or str(uuid.uuid4()), "language": "ru"},
        files={"file": (name, data, "application/octet-stream")},
    )


async def _guest_request(web_hotel: WebHotel) -> requests_api.ServiceRequestRead:
    """Заявка, оформленная ходом в веб-диалоге гостя (`request_origins`, ADR-011)."""
    with tenant_context(web_hotel.tenant_id):
        async with session_scope() as session:
            conversation_id = await session.scalar(
                select(Conversation.id).where(Conversation.channel == "web")
            )
        assert conversation_id is not None
        category = (await requests_api.list_categories())[0]
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="кран течёт",
            )
        )
        await record_request_origin(created.id, conversation_id)
    return created


def _stored_keys(storage: FileStoragePort) -> list[str]:
    keys: list[str] = cast(Any, storage).keys()
    return keys


async def test_photo_reaches_the_request_and_comes_back_to_the_guest(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    request = await _guest_request(web_hotel)

    sent = await _send(client)
    body = sent.json()
    image = await client.get(body["photo"]["photo_url"])
    feed = (await client.get(f"{CHAT}/messages")).json()["messages"]

    assert sent.status_code == 200, sent.text
    assert body["replies"] == [
        f"Фото приложили к заявке #{request.daily_number} — служба его увидит."
    ]
    assert body["duplicate"] is False
    with tenant_context(web_hotel.tenant_id):
        photos = await requests_api.list_request_photo_ids([request.id])
    file_id = body["photo"]["photo_url"].rsplit("/", 1)[1]
    assert [str(f) for f in photos[request.id]] == [file_id]
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    assert image.headers["cache-control"] == "private, max-age=3600"
    assert [(m["direction"], m["photo_url"] is not None) for m in feed] == [
        ("inbound", True),
        ("outbound", False),
    ]
    assert feed[0]["client_message_id"] == body["photo"]["client_message_id"]


async def test_repeat_of_the_same_key_stores_one_file(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    key = str(uuid.uuid4())

    first = (await _send(client, key=key)).json()
    second = (await _send(client, key=key)).json()

    assert second["duplicate"] is True and second["replies"] == []
    assert second["photo"]["id"] == first["photo"]["id"]
    assert len(_stored_keys(fake_file_storage)) == 1


async def test_text_dressed_as_jpg_is_refused_with_warning(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)

    with capture_logs() as logs:
        response = await _send(client, data=b"not a photo at all", name="tap.jpg")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "ERR-FILES-002"
    assert any(
        log["event"] == "guest_photo_rejected" and log["log_level"] == "warning" for log in logs
    )
    assert _stored_keys(fake_file_storage) == []


async def test_photos_over_the_hourly_limit_are_429(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos_router, "PHOTOS_PER_WINDOW", 2)
    await _sign_in(client, web_hotel)

    statuses = [(await _send(client)).status_code for _ in range(3)]
    over = await _send(client)

    assert statuses == [200, 200, 429]
    assert over.json()["error"]["code"] == "ERR-WEB-007"


async def test_guest_gets_only_photos_of_own_dialog(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    with tenant_context(web_hotel.tenant_id):
        foreign = await store_upload(FilePurpose.REQUEST_PHOTO, PNG, "other.png")

    for file_id in (foreign.id, uuid.uuid4()):
        response = await client.get(f"{PHOTOS}/{file_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ERR-FILES-004"


async def test_without_session_photo_is_refused(
    client: AsyncClient, photos_on: None, fake_file_storage: FileStoragePort
) -> None:
    response = await _send(client)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR-WEB-002"
    assert _stored_keys(fake_file_storage) == []


@pytest.mark.parametrize(
    "env",
    [
        {"ENABLE_GUEST_REQUEST_PHOTOS": "false", "ENABLE_SERVICE_REQUESTS": "true"},
        {"ENABLE_GUEST_REQUEST_PHOTOS": "true", "ENABLE_SERVICE_REQUESTS": "false"},
    ],
)
async def test_photos_off_means_no_camera_and_404(
    client: AsyncClient,
    web_hotel: WebHotel,
    fake_file_storage: FileStoragePort,
    env: dict[str, str],
) -> None:
    switch = _switch(**env)
    next(switch)
    try:
        page = await client.get(CHAT)
        await _sign_in(client, web_hotel)
        sent = await _send(client)
        read = await client.get(f"{PHOTOS}/{uuid.uuid4()}")
    finally:
        next(switch, None)

    assert 'id="photo-btn"' not in page.text and "photoScript" not in page.text
    for response in (sent, read):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ERR-WEB-006"
    assert _stored_keys(fake_file_storage) == []


async def test_camera_on_the_page_when_photos_are_on(client: AsyncClient, photos_on: None) -> None:
    page = await client.get(CHAT)
    script = await client.get(widget.URL)

    assert 'id="photo-btn"' in page.text and widget.URL in page.text
    assert 'accept="image/jpeg,image/png,image/webp"' in page.text
    assert "capture" not in page.text.split('id="photo-file"')[1].split(">")[0]
    # Надписи отказов — в файле фото, страница их не везёт (вес, #746).
    assert "Попробуйте другой снимок" not in page.text
    assert script.status_code == 200 and "Попробуйте другой снимок" in script.text
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_lost_race_of_two_repeats_removes_its_own_file(
    client: AsyncClient,
    web_hotel: WebHotel,
    photos_on: None,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _sign_in(client, web_hotel)
    key = str(uuid.uuid4())
    first = (await _send(client, key=key)).json()
    real_find = guest_photo.find_inbound_photo
    calls: list[str] = []

    async def late_find(conversation_id: uuid.UUID, idempotency_key: str) -> Any:
        # Повтор не видит первую запись до узла — как второй запрос, пришедший разом.
        calls.append(idempotency_key)
        return None if len(calls) == 1 else await real_find(conversation_id, idempotency_key)

    monkeypatch.setattr(photos_router, "find_inbound_photo", late_find)
    second = (await _send(client, key=key)).json()

    assert second["duplicate"] is True and second["photo"]["id"] == first["photo"]["id"]
    assert len(_stored_keys(fake_file_storage)) == 1

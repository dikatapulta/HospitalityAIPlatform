"""Форма реквизитов для АВР в веб-чате (spec 0049 §7 п. 4, Р-2, Р-7…Р-9).

Тесты DoD: без сессии — 401; выключатель или службы «Бухгалтерия» нет — 404 и
скрепки нет; тело больше потолка — 413 до разбора; отказ файла не оставляет
файлов; «готово» — одно сообщение на проживание, «вернули» — на запрос, на
языке запроса; отмена гостем не пишет.

F811 отключён на файл: фикстура-параметр обязана называться как
реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.web.documents import messages
from hospitality.channels.web.documents import router as documents_router
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.modules.documents import api as documents_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)

CHAT = f"/g/{HOTEL_SLUG}/{ROOM}"
FORM = f"{CHAT}/documents"
PDF = b"%PDF-1.4\nBIN 940140000385\n"
# Браузер с `FormData` шлёт multipart всегда, httpx без файлов — urlencoded:
# пустая часть без имени и байтов — как пустой выбор файла (её пропускает разбор).
NO_FILES = [("files", ("", b"", "application/octet-stream"))]


@pytest.fixture
def documents_switch() -> Iterator[pytest.MonkeyPatch]:
    """Выключатель Р-7 включён; тест может выключить его тем же патчем."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_GUEST_DOCUMENTS", "true")
    get_settings.cache_clear()
    yield patcher
    patcher.undo()
    get_settings.cache_clear()


@pytest.fixture
async def accounting(web_hotel: WebHotel) -> uuid.UUID:
    with tenant_context(web_hotel.tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=ACCOUNTING_SERVICE_KEY, name="Бухгалтерия")
        )
    return category.id


@pytest.fixture
async def client(web_hotel: WebHotel) -> AsyncIterator[AsyncClient]:
    del web_hotel  # нужен ради тенанта и заселённой комнаты
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


async def _sign_in(client: AsyncClient, web_hotel: WebHotel) -> None:
    response = await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
    assert response.status_code == 200, response.text


def _stored_keys(storage: FileStoragePort) -> list[str]:
    """Ключи Fake-хранилища: тип адаптера каналу не виден (контракт 2 import-linter)."""
    keys: list[str] = cast(Any, storage).keys()
    return keys


def _form(**fields: str) -> dict[str, str]:
    return {"client_key": uuid.uuid4().hex, "language": "ru-RU", **fields}


async def _chat_texts(client: AsyncClient) -> list[str]:
    response = await client.get(f"{CHAT}/messages")
    assert response.status_code == 200, response.text
    return [m["text"] for m in response.json()["messages"] if m["direction"] == "outbound"]


async def _act_requests(web_hotel: WebHotel) -> list[documents_api.ActRequestRow]:
    with tenant_context(web_hotel.tenant_id):
        return await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1), created_before=utc_now() + timedelta(days=1)
        )


async def _close(
    web_hotel: WebHotel,
    request_id: uuid.UUID,
    status: requests_api.RequestStatus,
    *,
    note: str | None = None,
    initiator: requests_api.RequestInitiator = requests_api.RequestInitiator.STAFF,
) -> None:
    """Сменить статус заявки и доставить событие подписчику — как это сделал бы воркер."""
    with tenant_context(web_hotel.tenant_id):
        before = await requests_api.get_request(request_id)
        await requests_api.change_request_status(
            request_id, status, resolution_note=note, initiator=initiator
        )
        await messages.on_request_status_changed(
            requests_api.RequestStatusChanged(
                request_id=request_id,
                old_status=before.status,
                new_status=status,
                initiator=initiator,
            )
        )


async def test_without_session_form_redirects_and_actions_are_401(
    client: AsyncClient, accounting: uuid.UUID, documents_switch: pytest.MonkeyPatch
) -> None:
    page = await client.get(FORM)
    data = await client.get(f"{FORM}/data")
    sent = await client.post(FORM, data=_form(bill_to="ТОО Ромашка"), files=NO_FILES)

    assert (page.status_code, page.headers["location"]) == (303, CHAT)
    assert data.status_code == 401
    assert sent.status_code == 401
    assert sent.json()["error"]["code"] == "ERR-WEB-002"


@pytest.mark.parametrize("missing", ["switch", "accounting"])
async def test_switch_off_or_no_accounting_means_no_clip_and_404(
    client: AsyncClient,
    web_hotel: WebHotel,
    documents_switch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    if missing == "switch":
        with tenant_context(web_hotel.tenant_id):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=ACCOUNTING_SERVICE_KEY, name="Бухгалтерия")
            )
        documents_switch.setenv("ENABLE_GUEST_DOCUMENTS", "false")
        get_settings.cache_clear()
    await _sign_in(client, web_hotel)

    chat = await client.get(CHAT)
    page = await client.get(FORM)
    sent = await client.post(FORM, data=_form(bill_to="ТОО Ромашка"), files=NO_FILES)

    assert 'id="clip"' not in chat.text
    assert "__DOCUMENTS_CLIP__" not in chat.text and "__DOCUMENTS_HINT__" not in chat.text
    for response in (page, sent):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ERR-DOCUMENTS-005"


async def test_clip_and_form_where_enabled(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
) -> None:
    await _sign_in(client, web_hotel)

    chat = await client.get(CHAT)
    page = await client.get(FORM)
    data = await client.get(f"{FORM}/data", params={"language": "kk-KZ"})

    assert 'id="clip"' in chat.text and "Қыстырғыш" in chat.text
    assert page.status_code == 200 and "documents-texts" in page.text
    assert data.status_code == 200
    body = data.json()
    assert (body["language"], body["room_number"]) == ("kk", ROOM)
    assert body["check_out_label"].split()[1] in {"қаңтар", "ақпан", "наурыз", "сәуір", "мамыр",
        "маусым", "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан"}  # fmt: skip


async def test_submit_creates_request_and_one_received_message_per_form(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    form = _form(bill_to="ТОО «Ромашка»", note="БИН:\r\n940140000385")
    files = [("files", ("rekv.pdf", PDF, "application/pdf"))]

    first = await client.post(FORM, data=form, files=files)
    again = await client.post(FORM, data=form, files=files)

    assert first.status_code == 200, first.text
    assert first.json()["files_count"] == 1
    assert again.status_code == 200 and again.json() == first.json()
    [row] = await _act_requests(web_hotel)
    assert (row.bill_to, row.guest_language, row.room_number) == ("ТОО «Ромашка»", "ru", ROOM)
    assert len(_stored_keys(fake_file_storage)) == 1
    with tenant_context(web_hotel.tenant_id):
        request = await requests_api.get_request(row.request_id)
    assert request.category_id == accounting
    assert request.origin is requests_api.ServiceRequestOrigin.GUEST_FORM
    texts = await _chat_texts(client)
    received = [text for text in texts if text.startswith("📄 Реквизиты получены")]
    assert len(received) == 1
    assert first.json()["check_out_label"] in received[0]


async def test_empty_form_is_422_with_its_code(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)

    response = await client.post(FORM, data=_form(bill_to="  ", note=""), files=NO_FILES)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-DOCUMENTS-001"


async def test_body_over_ceiling_is_413_before_parsing(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _sign_in(client, web_hotel)
    monkeypatch.setattr(documents_router, "MAX_BODY_BYTES", 4096)

    response = await client.post(
        FORM, data=_form(), files=[("files", ("big.pdf", PDF + bytes(8192), "application/pdf"))]
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "ERR-DOCUMENTS-002"
    assert _stored_keys(fake_file_storage) == []
    assert await _act_requests(web_hotel) == []


async def test_rejected_file_leaves_no_files_and_names_its_position(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("files", ("rekv.pdf", PDF, "application/pdf")),
        ("files", ("photo.jpg", b"not a photo at all", "image/jpeg")),
    ]

    response = await client.post(FORM, data=_form(bill_to="ТОО Ромашка"), files=files)

    assert response.status_code == 415
    error: dict[str, Any] = response.json()["error"]
    assert (error["code"], error["details"]) == ("ERR-FILES-002", [{"file_position": 1}])
    assert _stored_keys(fake_file_storage) == []
    assert await _act_requests(web_hotel) == []


async def test_ready_is_one_message_per_stay_returned_one_per_request(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    for bill_to in ("ТОО Ромашка", "ТОО Ромашка", "ТОО Василёк"):
        sent = await client.post(
            FORM, data=_form(bill_to=bill_to, language="en-GB"), files=NO_FILES
        )
        assert sent.status_code == 200, sent.text
    third, second, first = await _act_requests(web_hotel)  # новые сверху

    # Дубль закрыли «Готово» оба — гость получает одно сообщение (Р-2).
    for row in (first, second):
        await _close(web_hotel, row.request_id, requests_api.RequestStatus.IN_PROGRESS)
        await _close(web_hotel, row.request_id, requests_api.RequestStatus.DONE, note="Счёт №17")
    await _close(web_hotel, third.request_id, requests_api.RequestStatus.CANCELLED, note="нет БИН")

    texts = await _chat_texts(client)
    ready = [text for text in texts if text.startswith("📄 Accounting has prepared")]
    returned = [text for text in texts if text.startswith("📄 Accounting returned")]
    assert ready == [
        "📄 Accounting has prepared your closing documents. Please collect them at the front "
        "desk when you check out.\nNote from accounting: Счёт №17"
    ]
    assert returned == [
        "📄 Accounting returned your request: нет БИН. Please send the documents again — "
        "paperclip below."
    ]


async def test_cancel_by_guest_and_other_requests_write_nothing(
    client: AsyncClient,
    web_hotel: WebHotel,
    accounting: uuid.UUID,
    documents_switch: pytest.MonkeyPatch,
    fake_file_storage: FileStoragePort,
) -> None:
    await _sign_in(client, web_hotel)
    sent = await client.post(FORM, data=_form(bill_to="ТОО Ромашка"), files=NO_FILES)
    assert sent.status_code == 200, sent.text
    [row] = await _act_requests(web_hotel)
    with tenant_context(web_hotel.tenant_id):
        [housekeeping] = [c for c in await requests_api.list_categories() if c.key != "accounting"]
        other = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=housekeeping.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="Полотенца",
            )
        )
    before = await _chat_texts(client)

    await _close(
        web_hotel,
        row.request_id,
        requests_api.RequestStatus.CANCELLED,
        initiator=requests_api.RequestInitiator.GUEST,
    )
    await _close(web_hotel, other.id, requests_api.RequestStatus.CANCELLED, note="не нужно")

    assert await _chat_texts(client) == before

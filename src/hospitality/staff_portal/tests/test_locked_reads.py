"""Запертый кабинет читает только экран стойки (spec 0073 §2, #969).

Фоновый `fetch` с cookie запертой сессии раньше отдавал любую страницу владельца:
«Сотрудники», файлы реквизитов для АВР, переписку гостя. Теперь ему отвечают только
сигнал и списки очереди и досок, остальное — как истёкшей сессии.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.documents import api as documents_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_act_requests import _hotel, _submit
from hospitality.staff_portal.tests.test_conversations import _guest, _say
from hospitality.staff_portal.tests.test_session_lock import BACKGROUND, _idle_for, _lock_seconds
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)

BASE = f"/staff/{HOTEL_SLUG}"
LOGIN = f"{BASE}/login"
SIGNAL = f"{BASE}/api/requests/signal"
STAND_SCREEN = (f"{BASE}/requests/fragment", f"{BASE}/turnovers/fragment", f"{BASE}/rooms/fragment")


@pytest.fixture(autouse=True)
def _sections_on() -> Iterator[None]:
    """Разделы включены: закрывать должен запирание, а не выключатель."""
    patcher = pytest.MonkeyPatch()
    for flag in (
        "ENABLE_GUEST_DOCUMENTS",
        "ENABLE_STAFF_CONVERSATIONS",
        "ENABLE_TURNOVER_BOARD",
        "ENABLE_ROOM_BOARD",
    ):
        patcher.setenv(flag, "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _private_pages(portal_hotel: PortalHotel) -> list[str]:
    """«Сотрудники», файл реквизитов для АВР и лента диалога гостя — с настоящими данными."""
    stay_id = await _hotel(portal_hotel.tenant_id)
    act = await _submit(portal_hotel.tenant_id, stay_id, with_file="1")
    with tenant_context(portal_hotel.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1),
            created_before=utc_now() + timedelta(days=1),
        )
    _, conversation_id = await _guest(portal_hotel.tenant_id, "514")
    await _say(portal_hotel.tenant_id, conversation_id, "Где завтрак?", at=utc_now())
    return [
        f"{BASE}/team",
        f"{BASE}/act-requests/{act.id}/files/{row.files[0].file_id}",
        f"{BASE}/conversations/{conversation_id}",
    ]


@pytest.mark.usefixtures("fake_file_storage")
async def test_locked_background_read_gets_only_the_stand_screen(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    private = await _private_pages(portal_hotel)
    await submit_login(client, portal_hotel.login)
    for path in [*private, *STAND_SCREEN]:
        assert (await client.get(path, headers=BACKGROUND)).status_code == 200, path

    await _idle_for(_lock_seconds() + 60)

    for path in private:
        response = await client.get(path, headers=BACKGROUND)
        assert response.status_code == 303, path
        assert response.headers["location"] == LOGIN
    for path in STAND_SCREEN:
        assert (await client.get(path, headers=BACKGROUND)).status_code == 200, path
    signal = await client.get(SIGNAL, headers=BACKGROUND)
    assert signal.status_code == 200
    assert signal.json()["session"]["locked"] is True


async def test_locked_session_expires_after_its_own_term(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Сутки после запирания — и запертая истекает: сигнал 401, плашка «Вы вышли»."""
    await submit_login(client, portal_hotel.login)
    locked_ttl = get_settings().staff_session_locked_ttl_hours * 3600

    await _idle_for(_lock_seconds() + locked_ttl - 60)
    assert (await client.get(SIGNAL, headers=BACKGROUND)).status_code == 200

    await _idle_for(_lock_seconds() + locked_ttl + 60)
    expired = await client.get(SIGNAL, headers=BACKGROUND)
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "ERR-AUTH-002"

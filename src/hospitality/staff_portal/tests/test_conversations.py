"""Раздел «Диалоги» (spec 0067 §6; issue #767): переписка гостя с ботом, только чтение.

Права и выключатель, список (комната, превью, порядок, что не попадает), фильтры,
лента и её лог, ссылка «Открыть диалог» из окна заявки, изоляция тенанта (P-4).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from structlog.testing import capture_logs

from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.common.models import (
    ConversationEscalation,
    Message,
    MessageContentKind,
    MessageDirection,
)
from hospitality.channels.common.store import ensure_conversation, record_request_origin
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole, Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/conversations"
HOME = f"/staff/{HOTEL_SLUG}"
QUEUE = f"/staff/{HOTEL_SLUG}/requests"
ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture(autouse=True)
def _section_enabled() -> Iterator[None]:
    """Раздел включён во всех тестах файла — канон `test_stats`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_STAFF_CONVERSATIONS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _guest(tenant_id: uuid.UUID, room: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Заселённый гость, вошедший в веб-чат по коду: (stay_id, conversation_id)."""
    with tenant_context(tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + timedelta(days=2))
        )
        grant = await guests_api.start_guest_session(
            guests_api.GuestSessionStart(
                room_number=room,
                code=checked.access_code,
                identity_external_id=str(uuid.uuid4()),
                consent_version=CONSENT_VERSION,
            )
        )
        assert grant is not None
        conversation_id = await ensure_conversation(
            "web", str(grant.guest_identity_id), guest_identity_id=grant.guest_identity_id
        )
    return checked.stay.id, conversation_id


async def _say(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str | None,
    *,
    at: datetime,
    bot: bool = False,
) -> None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND if bot else MessageDirection.INBOUND,
                    content_kind=MessageContentKind.TEXT
                    if text is not None
                    else MessageContentKind.UNSUPPORTED,
                    text=text,
                    correlation_id="test",
                    created_at=at,
                )
            )


async def _login(
    client: AsyncClient,
    tenant_id: uuid.UUID,
    *,
    role: StaffRole = StaffRole.STAFF,
    services: tuple[str, ...] = (),
) -> uuid.UUID:
    login = unique_login()
    user_id = await create_staff_user(
        login, tenant_id=tenant_id, role=role, service_keys=services, display_name="Айгуль"
    )
    await submit_login(client, login)
    return user_id


async def test_rights_menu_and_switch(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6 пп. 1, 7: менеджер и ресепшен уровня «Сотрудник» видят пункт и страницу;
    старший и сотрудник уборки — 403 и без пункта. Выключатель снят — 404 и пункта нет."""
    await submit_login(client, portal_hotel.login)
    page = await client.get(PAGE)
    assert page.status_code == 200
    assert "<h1>Диалоги</h1>" in page.text
    assert "Эту страницу видят только ресепшен и менеджер." in page.text
    assert "Диалогов пока нет. Они появятся, когда гости напишут в чат." in page.text
    assert f'href="{PAGE}"' in (await client.get(HOME)).text

    await _login(second_client, portal_hotel.tenant_id, services=RECEPTION)
    assert (await second_client.get(PAGE)).status_code == 200
    assert f'href="{PAGE}"' in (await second_client.get(HOME)).text

    for role in (StaffRole.SENIOR, StaffRole.STAFF):
        second_client.cookies.clear()
        await _login(second_client, portal_hotel.tenant_id, role=role, services=("housekeeping",))
        assert (await second_client.get(PAGE)).status_code == 403
        assert f'href="{PAGE}"' not in (await second_client.get(HOME)).text

    _, conversation_id = await _guest(portal_hotel.tenant_id, "704")
    monkeypatch.setenv("ENABLE_STAFF_CONVERSATIONS", "false")
    get_settings.cache_clear()
    assert (await client.get(PAGE)).status_code == 404
    assert (await client.get(f"{PAGE}/{conversation_id}")).status_code == 404
    assert f'href="{PAGE}"' not in (await client.get(HOME)).text


async def test_list_shows_web_dialogs_newest_first(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 2: комната из проживания (переселение — новый номер), превью и «Бот:»,
    свежие сверху; диалог без сообщений и Telegram-чат в список не попадают."""
    now = utc_now()
    stay_704, first = await _guest(portal_hotel.tenant_id, "704")
    await _say(portal_hotel.tenant_id, first, "Где завтрак?", at=now - timedelta(hours=2))
    await _say(
        portal_hotel.tenant_id, first, "Завтрак с 7 до 10", at=now - timedelta(hours=1), bot=True
    )
    _, second = await _guest(portal_hotel.tenant_id, "512")
    await _say(portal_hotel.tenant_id, second, "Принесите   полотенце\nпожалуйста", at=now)
    await _guest(portal_hotel.tenant_id, "300")
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.move_stay(stay_704, "715")
        staff_chat = await ensure_conversation("telegram", "-100777")
    await _say(portal_hotel.tenant_id, staff_chat, "Служебное уведомление", at=now)
    await submit_login(client, portal_hotel.login)

    body = (await client.get(PAGE)).text

    assert body.index("Принесите полотенце пожалуйста") < body.index("Бот: Завтрак с 7 до 10")
    assert '<span class="sr-only">Комната </span>715</span>' in body
    assert '<span class="sr-only">Комната </span>512</span>' in body
    assert "Где завтрак?" not in body
    assert ">300<" not in body
    assert "Служебное уведомление" not in body
    assert f'href="{PAGE}/{first}"' in body


async def test_filters_by_room_and_hotel_day(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 3: комната — и у выехавшего гостя; дата — сутки отеля; кривая дата — без фильтра."""
    await store_hotel_config(portal_hotel.tenant_id)
    today = utc_now().astimezone(ALMATY).date()
    stay_id, left = await _guest(portal_hotel.tenant_id, "704")
    # 23:30 вчера по Алматы — это вчерашние сутки отеля, хотя в UTC может быть «сегодня».
    late_yesterday = datetime.combine(today - timedelta(days=1), datetime.min.time(), ALMATY)
    await _say(
        portal_hotel.tenant_id,
        left,
        "Вчерашний вопрос",
        at=late_yesterday.replace(hour=23, minute=30),
    )
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_out(stay_id)
    _, stays = await _guest(portal_hotel.tenant_id, "512")
    await _say(portal_hotel.tenant_id, stays, "Сегодняшний вопрос", at=utc_now())
    await submit_login(client, portal_hotel.login)

    by_room = (await client.get(PAGE, params={"room": " 704 "})).text
    assert "Вчерашний вопрос" in by_room and "Сегодняшний вопрос" not in by_room
    assert 'value="704"' in by_room and "Сбросить" in by_room
    nothing = (await client.get(PAGE, params={"room": "999"})).text
    assert "По этому фильтру диалогов нет." in nothing

    by_day = (await client.get(PAGE, params={"date": today.isoformat()})).text
    assert "Сегодняшний вопрос" in by_day and "Вчерашний вопрос" not in by_day
    yesterday = await client.get(PAGE, params={"date": (today - timedelta(days=1)).isoformat()})
    assert "Вчерашний вопрос" in yesterday.text and "Сегодняшний вопрос" not in yesterday.text

    broken = await client.get(PAGE, params={"date": "2026-13-40"})
    assert broken.status_code == 200
    assert "Вчерашний вопрос" in broken.text and "Сегодняшний вопрос" in broken.text


async def test_feed_shows_messages_and_logs_opening_without_text(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 пп. 4–5: порядок, подписи, не-текст, экранирование; лог — id без текста."""
    user_id = await _login(client, portal_hotel.tenant_id, services=RECEPTION)
    now = utc_now()
    _, conversation_id = await _guest(portal_hotel.tenant_id, "704")
    await _say(
        portal_hotel.tenant_id,
        conversation_id,
        "Сломался <b>кран</b>",
        at=now - timedelta(minutes=3),
    )
    await _say(
        portal_hotel.tenant_id,
        conversation_id,
        "Передал инженеру",
        at=now - timedelta(minutes=2),
        bot=True,
    )
    await _say(portal_hotel.tenant_id, conversation_id, None, at=now - timedelta(minutes=1))

    with capture_logs() as logs:
        page = await client.get(f"{PAGE}/{conversation_id}")

    assert page.status_code == 200
    body = page.text
    assert "<h1>Комната 704</h1>" in body
    assert "Только просмотр." in body and "← Все диалоги" in body
    assert body.index("Сломался &lt;b&gt;кран&lt;/b&gt;") < body.index("Передал инженеру")
    assert body.index("Передал инженеру") < body.index("Не текст — бот такое не читает")
    assert "conv-msg conv-msg-guest" in body and "conv-msg conv-msg-bot" in body
    # Только чтение: в ленте нет ни формы, ни поля ввода (формы каркаса — выход и
    # «Новая заявка» — не в ней).
    feed = body[body.index('<ol class="conv-feed">') : body.index("</ol>")]
    assert "<form" not in feed and "<textarea" not in feed and "<input" not in feed
    opened = [entry for entry in logs if entry["event"] == "staff.conversation_opened"]
    assert opened == [
        {
            "event": "staff.conversation_opened",
            "log_level": "info",
            "module": "hospitality.staff_portal.conversations",
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "messages": 3,
        }
    ]
    assert "кран" not in json.dumps(logs, default=str, ensure_ascii=False)


async def test_feed_is_404_for_foreign_telegram_and_bad_ids(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§6 п. 4: диалог соседнего отеля (RLS), Telegram-чат и кривой id — 404."""
    async with platform_session_scope() as session:
        neighbour = Tenant(slug=f"neighbour-{uuid.uuid4().hex[:6]}", name="Соседний отель")
        session.add(neighbour)
        await session.flush()
        neighbour_id = neighbour.id
    _, foreign = await _guest(neighbour_id, "704")
    await _say(neighbour_id, foreign, "Вопрос соседа", at=utc_now())
    with tenant_context(portal_hotel.tenant_id):
        telegram = await ensure_conversation("telegram", "-100555")
    await submit_login(client, portal_hotel.login)

    for path in (f"{PAGE}/{foreign}", f"{PAGE}/{telegram}", f"{PAGE}/not-a-uuid"):
        assert (await client.get(path)).status_code == 404
    assert "Вопрос соседа" not in (await client.get(PAGE)).text


async def test_request_window_links_to_its_dialog(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6 пп. 6–7: заявка бота и карточка эскалации — ссылка на свой диалог; ручная
    заявка — без ссылки; сотрудник уборки и выключенный раздел — без ссылки."""
    tenant_id = portal_hotel.tenant_id
    _, bot_dialog = await _guest(tenant_id, "704")
    _, escalated_dialog = await _guest(tenant_id, "512")
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )

        async def create(origin: requests_api.ServiceRequestOrigin, room: str) -> uuid.UUID:
            created = await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=category.id, origin=origin, summary="Полотенца", room_number=room
                )
            )
            return created.id

        from_bot = await create(requests_api.ServiceRequestOrigin.GUEST_CHAT, "704")
        escalation = await create(requests_api.ServiceRequestOrigin.GUEST_CHAT, "512")
        await create(requests_api.ServiceRequestOrigin.STAFF_MANUAL, "300")
        await record_request_origin(from_bot, bot_dialog)
        async with session_scope() as session:
            session.add(
                ConversationEscalation(
                    conversation_id=escalated_dialog, reason="emergency", request_id=escalation
                )
            )
    await submit_login(client, portal_hotel.login)

    body = (await client.get(QUEUE)).text
    assert f'href="{PAGE}/{bot_dialog}">Открыть диалог</a>' in body
    assert f'href="{PAGE}/{escalated_dialog}">Открыть диалог</a>' in body
    assert body.count(">Открыть диалог</a>") == 2

    await _login(second_client, tenant_id, services=("housekeeping",))
    housekeeping_view = (await second_client.get(QUEUE)).text
    assert "Полотенца" in housekeeping_view and "Открыть диалог" not in housekeeping_view

    monkeypatch.setenv("ENABLE_STAFF_CONVERSATIONS", "false")
    get_settings.cache_clear()
    assert "Открыть диалог" not in (await client.get(QUEUE)).text

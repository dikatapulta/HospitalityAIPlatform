"""Раздел «Диалоги» (spec 0067, issue #767): переписка гостя с ботом в веб-чате.

Только чтение — ни ответа гостю, ни правок. Диалоги и сообщения читает канал
(`channels/web/conversation_view.py`), комнату — модуль guests по идентичности
привязки (§2.1); здесь — права, фильтр, подписи и время по поясу отеля.

Право — ресепшен любого уровня и менеджер (§2.5): та же граница, что у страниц
(`require_role(manager, or_reception=True)` в `router.py`); `can_view` — для меню
и ссылки «Открыть диалог» в окне заявки, чтобы они не вели в «Нет доступа».
Тексты гостя в лог не идут: открытие ленты — строка `staff.conversation_opened`
с id, без текста (§2.5).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final

from hospitality.channels.common.models import MessageDirection
from hospitality.channels.web import conversation_view
from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience, StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import checkin

logger = get_logger(module=__name__)

# Пределы страниц (§2.2, §2.3): больше — подсказка, а не вторая страница.
LIST_LIMIT: Final = 50
FEED_LIMIT: Final = 200
PREVIEW_CHARS: Final = 80
# Потолок поля «Комната» — длина колонки `stays.room_number` (String(20)).
_ROOM_MAX_LENGTH: Final = 20
NOT_TEXT_LABEL: Final = "Не текст — бот такое не читает"


def section_enabled() -> bool:
    """`ENABLE_STAFF_CONVERSATIONS` (§2.6): одна проверка на меню, ссылку и страницы."""
    return get_settings().enable_staff_conversations


def can_view(staff: StaffAudience) -> bool:
    """Ресепшен любого уровня или менеджер (§2.5)."""
    return staff.is_reception or staff.role_key is StaffRole.MANAGER


def list_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/conversations"


def conversation_path(tenant_slug: str, conversation_id: uuid.UUID) -> str:
    return f"{list_path(tenant_slug)}/{conversation_id}"


@dataclass(frozen=True)
class ConversationFilter:
    """Фильтр списка (§2.2): номер комнаты целиком и сутки отеля."""

    room: str = ""
    day: date | None = None


def parse_filter(room: str | None, day: str | None) -> ConversationFilter:
    """Фильтр из query-параметров; непонятная дата — фильтра нет, без 422
    (канон `summary.parse_summary_day`): ссылку с опечаткой мог прислать коллега."""
    try:
        parsed = date.fromisoformat(day.strip()) if day and day.strip() else None
    except ValueError:
        parsed = None
    return ConversationFilter(room=(room or "").strip()[:_ROOM_MAX_LENGTH], day=parsed)


def _day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Сутки отеля `[полночь, следующая полночь)` в UTC (FOUNDATION §9)."""
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def time_label(moment: datetime, *, now: datetime, zone: tzinfo) -> str:
    """Сегодня по отелю — «14:05», иначе «02.11 14:05»."""
    local = moment.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def _preview(message: conversation_view.MessageRow) -> str:
    """Последнее сообщение в одну строку до `PREVIEW_CHARS`; ответ бота — «Бот: …»."""
    text = NOT_TEXT_LABEL if message.text is None else " ".join(message.text.split())
    if len(text) > PREVIEW_CHARS:
        text = text[: PREVIEW_CHARS - 1].rstrip() + "…"
    return f"Бот: {text}" if message.direction is MessageDirection.OUTBOUND else text


async def _rooms(identity_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    return await guests_api.rooms_for_identities(
        {identity_id for identity_id in identity_ids if identity_id is not None}
    )


def _room(rooms: dict[uuid.UUID, str], identity_id: uuid.UUID | None) -> str | None:
    return None if identity_id is None else rooms.get(identity_id)


def _page_base(staff: StaffContext) -> dict[str, Any]:
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "list_href": list_path(staff.tenant_slug),
    }


async def build_list_context(staff: StaffContext, found: ConversationFilter) -> dict[str, Any]:
    """Контекст `conversations.html`. Внутри контекста тенанта запроса: диалоги и
    проживания читаются под RLS, чужих строк здесь нет."""
    zone = await checkin.hotel_zone(staff)
    now = utc_now()
    identity_ids = await guests_api.identities_in_room(found.room) if found.room else None
    since, until = _day_bounds(found.day, zone) if found.day else (None, None)
    rows = await conversation_view.list_web_conversations(
        identity_ids=identity_ids, since=since, until=until, limit=LIST_LIMIT + 1
    )
    more, rows = len(rows) > LIST_LIMIT, rows[:LIST_LIMIT]
    rooms = await _rooms(row.guest_identity_id for row in rows)
    return _page_base(staff) | {
        "filter_room": found.room,
        "filter_date": found.day.isoformat() if found.day else "",
        "today": now.astimezone(zone).date().isoformat(),
        "filtered": bool(found.room or found.day),
        "rows": [
            {
                "href": conversation_path(staff.tenant_slug, row.id),
                "room": _room(rooms, row.guest_identity_id) or "—",
                "preview": _preview(row.last_message),
                "from_bot": row.last_message.direction is MessageDirection.OUTBOUND,
                "when": time_label(row.last_message.created_at, now=now, zone=zone),
            }
            for row in rows
        ],
        "more": more,
        "list_limit": LIST_LIMIT,
    }


async def build_feed_context(
    staff: StaffContext, conversation_id: uuid.UUID
) -> dict[str, Any] | None:
    """Контекст `conversation.html` и строка лога открытия; None — диалога нет
    (чужой отель, не веб-чат, неизвестный id) → 404."""
    head = await conversation_view.get_web_conversation(conversation_id)
    if head is None:
        return None
    messages = await conversation_view.list_conversation_messages(head.id, limit=FEED_LIMIT + 1)
    more, messages = len(messages) > FEED_LIMIT, messages[-FEED_LIMIT:]
    room = _room(await _rooms([head.guest_identity_id]), head.guest_identity_id)
    zone = await checkin.hotel_zone(staff)
    now = utc_now()
    logger.info(
        "staff.conversation_opened",
        user_id=str(staff.user_id),
        conversation_id=str(head.id),
        messages=len(messages),
    )
    return _page_base(staff) | {
        "title": f"Комната {room}" if room else "Диалог без комнаты",
        "started_label": head.created_at.astimezone(zone).strftime("%d.%m в %H:%M"),
        "messages": [
            {
                "from_guest": message.direction is MessageDirection.INBOUND,
                "text": message.text,
                "when": time_label(message.created_at, now=now, zone=zone),
            }
            for message in messages
        ],
        "not_text_label": NOT_TEXT_LABEL,
        "more": more,
        "feed_limit": FEED_LIMIT,
    }


async def conversation_hrefs(
    staff: StaffContext, request_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Ссылки «Открыть диалог» для окна заявки (§2.4): только тому, кто вправе
    открыть ленту, и только при включённом разделе — иначе пусто без запроса."""
    if not (section_enabled() and can_view(staff)):
        return {}
    found = await conversation_view.web_conversations_for_requests(list(request_ids))
    return {
        request_id: conversation_path(staff.tenant_slug, conversation_id)
        for request_id, conversation_id in found.items()
    }

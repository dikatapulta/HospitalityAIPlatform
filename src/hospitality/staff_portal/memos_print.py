"""Страница печати записки (spec 0077 §5, §9; #936, PR 4б-3): лист A4 с листом подписей.

PDF делает браузер («Сохранить как PDF» в окне печати) — серверной генерации и новой
зависимости нет; макет бумаги — `@media print` в `static/memos.css`. Печатается только
отправленная запись: у черновика нет номера и подписей, и на бумаге его легко принять за
настоящую записку (черновик — 404, как неизвестный адрес). Данные и право — те же, что
у карточки (`memos_api.get_memo`, чужая — ERR-MEMO-001): открытие печати — тоже просмотр,
строку журнала пишет модуль. Сами просмотры на бумагу не идут — это журнал кабинета.
Правка текста в базе после отправки на бумаге названа той же строкой, что на карточке:
иначе лист выдал бы подписи под прежним текстом за подписи под этим.
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any

from fastapi import HTTPException

from hospitality.modules.memos import api as memos_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.staff_portal import memos_card
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.memos import card_href
from hospitality.staff_portal.memos_people import directory, person_name


async def print_context(staff: StaffContext, memo_id: uuid.UUID, zone: tzinfo) -> dict[str, Any]:
    """Контекст `memo_print.html`: шапка, текст, решение, лист подписей и строка §9."""
    card = await memos_api.get_memo(staff, memo_id)
    if card.sent_at is None or card.number is None:
        raise HTTPException(status_code=404)
    people = await directory(staff)
    return {
        "tenant_name": staff.tenant_name,
        "kind": texts.KIND[card.kind],
        "number": card.number,
        "title": card.title,
        "body": card.body,
        "status": texts.status_text(card.kind, card.status),
        "author": person_name(people, card.author_id),
        "addressee": person_name(people, card.addressee_id),
        "sent": format_local(card.sent_at, zone),
        "based_on_number": card.based_on_number,
        "based_on_label": texts.BASED_ON[card.kind],
        "decision": memos_card.decision_block(card, people, zone),
        "content_changed": not card.content_intact,
        "content_changed_text": texts.CONTENT_CHANGED,
        "sheet": memos_card.sheet_rows(card, people, zone),
        "footer": texts.PRINT_FOOTER.format(day=format_local(utc_now(), zone)[:10]),
        "card_href": card_href(staff, card.id),
        "print_button": texts.PRINT_BUTTON,
        "print_hint": texts.PRINT_HINT,
    }

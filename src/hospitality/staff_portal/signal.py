"""Данные сигнала кабинета (spec 0039 §3, issue #381).

Сигнал — `static/signal.js` на каждой странице отеля: звук, счётчик в заголовке
вкладки и уведомление на компьютере о невзятых заявках. У отеля «только
кабинет» (staff-бота нет) это единственный путь, которым заявка зовёт
персонал, поэтому опрос идёт и на фоновой вкладке.

Ответ — только невзятые (`new`), не больше `_SIGNAL_LIMIT` новейших и **без
текста гостя**: уведомление на экране компьютера читает любой, кто стоит рядом,
а текст есть в карточке очереди. «Просрочена» и возраст заявки — те же
`queue.is_overdue` / `queue.age_label`, что у карточки (P-12): сигнал и очередь
обязаны говорить об одних заявках одними словами.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.staff_portal import browser, queue

# Страховка от неограниченного скана (канон `list_unclaimed_requests`); у пилота
# ~50–120 заявок в сутки, полсотни невзятых разом — уже авария, а не очередь.
_SIGNAL_LIMIT: Final = 50


class UnclaimedRequestView(BaseModel):
    """Невзятая заявка для сигнала: номер, комната, служба и метки — без текста гостя."""

    id: str
    # Номера нет у строк до миграции 0010 — очередь пишет «—», уведомление
    # выходит без номера.
    daily_number: int | None
    room_number: str | None
    category_name: str
    is_urgent: bool
    is_overdue: bool
    # Возраст словами карточки очереди («15 мин», «3 ч») — для уведомления о
    # просрочке «Заявка #12 ждёт уже 15 мин» (spec 0039 §4).
    age_label: str


class SignalView(BaseModel):
    """Ответ `GET …/api/requests/signal`, новые сверху."""

    unclaimed: list[UnclaimedRequestView]


def signal_context(tenant_slug: str) -> dict[str, str]:
    """Адреса для каркаса страницы отеля: наличие этого словаря в контексте и
    подключает `signal.js` (`layout.html`); у страниц входа и приглашения его нет."""
    return {
        "url": f"/staff/{tenant_slug}/api/requests/signal",
        "queue_url": f"/staff/{tenant_slug}/requests",
        "login_url": browser.hotel_login_path(tenant_slug),
    }


async def build_signal(staff: StaffContext) -> SignalView:
    """Невзятые заявки текущего тенанта (RLS ставит звено TenantResolver)."""
    now = utc_now()
    requests = await requests_api.list_unclaimed_requests(created_before=now, limit=_SIGNAL_LIMIT)
    if not requests:
        return SignalView(unclaimed=[])
    categories = await requests_api.list_categories()
    category_names = {category.id: category.name for category in categories}
    category_keys = {category.id: category.key for category in categories}
    config = await queue.tenant_config(staff)
    return SignalView(
        unclaimed=[
            UnclaimedRequestView(
                id=str(request.id),
                daily_number=request.daily_number,
                room_number=request.room_number,
                category_name=category_names.get(request.category_id, "—"),
                is_urgent=request.is_urgent,
                is_overdue=queue.is_overdue(
                    request, category_keys.get(request.category_id), config, now
                ),
                age_label=queue.age_label(request.created_at, now),
            )
            for request in requests
        ]
    )

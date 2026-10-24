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

Звучит заявка не всем, а адресатам ступени лестницы (spec 0045 §4, `ladder.py`):
сотруднику и старшему — заявки своих служб с первой минуты (срез в SQL);
ресепшену и менеджеру — свои службы и всё, что поднялось на третью ступень.
Видят они в очереди по-прежнему все заявки — ступень решает только, зовёт ли.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_team import staffed_service_keys
from hospitality.shared.db import utc_now
from hospitality.staff_portal import browser, queue, scope
from hospitality.staff_portal.ladder import LadderStep, ladder_step, step_by_time

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
    # Заявку не взяли за два срока, и она зовёт смотрящего как третья ступень
    # (spec 0045 §4): «Никто не взял заявку #12». Только у ресепшена и
    # менеджера — службу третья ступень не зовёт заново.
    is_raised: bool = False


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
    """Невзятые заявки, которые зовут этого сотрудника (RLS ставит TenantResolver)."""
    now = utc_now()
    # Сотруднику службы категории нужны до запроса — срез по службам в SQL.
    categories = None if staff.sees_all_services else await requests_api.list_categories()
    allowed = None if categories is None else scope.allowed_category_ids(staff, categories)
    requests = await requests_api.list_unclaimed_requests(
        created_before=now, limit=_SIGNAL_LIMIT, category_ids=allowed
    )
    if not requests:
        return SignalView(unclaimed=[])
    if categories is None:
        categories = await requests_api.list_categories()
    category_names = {category.id: category.name for category in categories}
    category_keys = {category.id: category.key for category in categories}
    config = await queue.tenant_config(staff)
    views = [
        _view(request, category_names, category_keys.get(request.category_id), config, now)
        for request in requests
    ]
    if not staff.sees_all_services:
        return SignalView(unclaimed=views)
    staffed = await staffed_service_keys(staff.tenant_id)
    addressed: list[UnclaimedRequestView] = []
    for request, view in zip(requests, views, strict=True):
        key = category_keys.get(request.category_id)
        delay = config.reminder_delay_for(key) if config is not None else None
        age = now - request.created_at
        step = ladder_step(
            age=age,
            delay=delay,
            is_urgent=request.is_urgent,
            is_escalation=request.origin is requests_api.ServiceRequestOrigin.ESCALATION,
            service_staffed=key in staffed,
        )
        if key in staff.service_keys or step is LadderStep.RECEPTION:
            view.is_raised = step_by_time(age, delay) is LadderStep.RECEPTION
            addressed.append(view)
    return SignalView(unclaimed=addressed)


def _view(
    request: requests_api.ServiceRequestRead,
    category_names: dict[Any, str],
    category_key: str | None,
    config: TenantConfig | None,
    now: datetime,
) -> UnclaimedRequestView:
    return UnclaimedRequestView(
        id=str(request.id),
        daily_number=request.daily_number,
        room_number=request.room_number,
        category_name=category_names.get(request.category_id, "—"),
        is_urgent=request.is_urgent,
        is_overdue=queue.is_overdue(request, category_key, config, now),
        age_label=queue.age_label(request.created_at, now),
    )

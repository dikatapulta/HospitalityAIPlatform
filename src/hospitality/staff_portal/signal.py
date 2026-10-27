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

Второй список — номера доски «Выезды и уборка» (spec 0046 §7): хаускипингу —
выезд, старшему хаускипинга — убранный номер, ресепшену — замечание при проверке.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_team import staffed_service_keys
from hospitality.shared.db import utc_now
from hospitality.staff_portal import browser, queue, scope, turnovers
from hospitality.staff_portal.ladder import LadderStep, ladder_step, step_by_time

# Страховка от неограниченного скана (канон `list_unclaimed_requests`); у пилота
# ~50–120 заявок в сутки, полсотни невзятых разом — уже авария, а не очередь.
_SIGNAL_LIMIT: Final = 50

# Замечание зовёт ресепшен, пока гость, может быть, ещё у стойки (spec 0046 §7);
# позже сигнал его не присылает — текст остаётся на доске.
REMARK_SIGNAL_WINDOW: Final = timedelta(minutes=30)

TurnoverCallKind = Literal["inspect", "accept", "remark"]


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


class TurnoverCallView(BaseModel):
    """Номер с доски выездов, который зовёт этого сотрудника (spec 0046 §7)."""

    id: str
    room_number: str
    # inspect — «Проверьте номер» (путь в `reported`), accept — «Примите номер»
    # (`cleaned`), remark — замечание при проверке, ресепшену один раз.
    kind: TurnoverCallKind
    # Только у remark. Текст сотрудника, может пересказывать гостя: в JS — только
    # textContent, в логи — никогда (PII_REGISTRY).
    note: str | None = None


class SignalView(BaseModel):
    """Ответ `GET …/api/requests/signal`, новые сверху в обоих списках."""

    unclaimed: list[UnclaimedRequestView]
    turnovers: list[TurnoverCallView]


def signal_context(tenant_slug: str) -> dict[str, str]:
    """Адреса для каркаса страницы отеля: наличие этого словаря в контексте и
    подключает `signal.js` (`layout.html`); у страниц входа и приглашения его нет."""
    return {
        "url": f"/staff/{tenant_slug}/api/requests/signal",
        "queue_url": f"/staff/{tenant_slug}/requests",
        "turnovers_url": turnovers.board_path(tenant_slug),
        "login_url": browser.hotel_login_path(tenant_slug),
    }


async def build_signal(staff: StaffContext) -> SignalView:
    """Что зовёт этого сотрудника: заявки и номера доски (RLS ставит TenantResolver)."""
    now = utc_now()
    return SignalView(
        unclaimed=await _unclaimed(staff, now), turnovers=await _turnover_calls(staff, now)
    )


async def _turnover_calls(staff: StaffContext, now: datetime) -> list[TurnoverCallView]:
    """Номера доски, где следующий шаг за этим сотрудником (spec 0046 §7).

    Адресатов определяют права доски `turnovers.rights_for`: «Проверьте» — кому
    можно «Проверено» (хаускипинг), «Примите» — кому можно «Принять» (старший
    хаускипинга), замечание — кому можно «Отметить выезд» (ресепшен). Менеджера
    не зовут: права на всё у него — подстраховка, а сигнал звал бы его на каждый
    выезд отеля; как и с заявками, он слышит только застрявшее (spec 0045 §4).
    Доска выключена — пустой список без запроса в базу (spec 0046 §5).
    """
    if not turnovers.board_enabled() or staff.role_key is StaffRole.MANAGER:
        return []
    rights = turnovers.rights_for(staff)
    if not (rights.can_work or rights.can_accept or rights.can_report):
        return []
    open_turnovers = await housekeeping_api.list_open_turnovers(limit=turnovers.OPEN_LIMIT)
    remarks_after = now - REMARK_SIGNAL_WINDOW
    calls: list[TurnoverCallView] = []
    for turnover in reversed(open_turnovers):  # доска отдаёт давние сверху
        status = turnover.status
        if rights.can_work and status is housekeeping_api.TurnoverStatus.REPORTED:
            calls.append(_call(turnover, "inspect"))
        if rights.can_accept and status is housekeeping_api.TurnoverStatus.CLEANED:
            calls.append(_call(turnover, "accept"))
        inspected_at = turnover.inspected_at
        if (
            rights.can_report
            and turnover.inspection_note is not None
            and inspected_at is not None
            and inspected_at >= remarks_after
        ):
            calls.append(_call(turnover, "remark", note=turnover.inspection_note))
    return calls


def _call(
    turnover: housekeeping_api.RoomTurnoverRead,
    kind: TurnoverCallKind,
    *,
    note: str | None = None,
) -> TurnoverCallView:
    return TurnoverCallView(
        id=str(turnover.id), room_number=turnover.room_number, kind=kind, note=note
    )


async def _unclaimed(staff: StaffContext, now: datetime) -> list[UnclaimedRequestView]:
    """Невзятые заявки, которые зовут этого сотрудника (лестница spec 0045 §4)."""
    # Сотруднику службы категории нужны до запроса — срез по службам в SQL.
    categories = None if staff.sees_all_services else await requests_api.list_categories()
    allowed = None if categories is None else scope.allowed_category_ids(staff, categories)
    requests = await requests_api.list_unclaimed_requests(
        created_before=now, limit=_SIGNAL_LIMIT, category_ids=allowed
    )
    if not requests:
        return []
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
        return views
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
    return addressed


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

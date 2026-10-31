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

Третий — взятые и не закрытые дольше срока отеля, «долго в работе» (spec 0028 §8):
службе заявки, ресепшену и менеджеру — тем, кто видит заявку в очереди. Срок по
умолчанию выключен — список пуст без запроса заявок.

Этажи (spec 0053 §4, `floors.FloorRouting`): заявка уборки и выезд из номера,
у этажа которого есть команда, на первой ступени зовут только эту команду; через
срок `d` уборки — всю службу, как раньше. Видят их в очереди и на доске все.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.platform.staff_team import staffed_service_keys
from hospitality.shared.db import utc_now
from hospitality.staff_portal import browser, queue, scope, turnovers
from hospitality.staff_portal.floors import NO_ROUTING, FloorRouting
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
    # Служба заявки: переданная в другую службу невзятая звучит заново
    # (spec 0042 §2.4) — ключ «уже прозвучало» у рассылки пуша (spec 0050 §6).
    category_id: str
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
    # Заказ в номер (spec 0038 §5): уведомление — «Новый заказ #12».
    is_order: bool = False


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


class StalledRequestView(BaseModel):
    """Взятая и не закрытая дольше срока отеля (spec 0028 §8) — без текста гостя."""

    id: str
    daily_number: int | None
    room_number: str | None
    category_name: str
    # Сколько в работе — от взятия, словами карточки очереди: «Заявка #12 в
    # работе уже 4 ч». У строк до spec 0035 взятия нет — от создания.
    in_work_label: str


class SignalView(BaseModel):
    """Ответ `GET …/api/requests/signal`, новые сверху во всех списках."""

    unclaimed: list[UnclaimedRequestView]
    turnovers: list[TurnoverCallView]
    stalled: list[StalledRequestView]


def signal_context(tenant_slug: str) -> dict[str, str]:
    """Адреса для каркаса страницы отеля: наличие этого словаря в контексте и
    подключает `signal.js` (`layout.html`); у страниц входа и приглашения его нет."""
    return {
        "url": f"/staff/{tenant_slug}/api/requests/signal",
        "queue_url": f"/staff/{tenant_slug}/requests",
        "turnovers_url": turnovers.board_path(tenant_slug),
        "login_url": browser.hotel_login_path(tenant_slug),
    }


async def build_signal(staff: StaffAudience, routing: FloorRouting = NO_ROUTING) -> SignalView:
    """Что зовёт этого сотрудника: заявки и номера доски (RLS — контекст тенанта).

    Достаточно уровня и служб (`StaffAudience`) и раздачи этажей глазами
    сотрудника (`routing`): опрос вкладки передаёт сюда сотрудника запроса,
    рассылка пуша — группу сотрудников (spec 0050 §6, spec 0053 §4).
    """
    now = utc_now()
    # Конфиг — одним чтением на ответ: сроки нужны всем трём спискам.
    config = await queue.tenant_config(staff)
    return SignalView(
        unclaimed=await _unclaimed(staff, config, now, routing),
        turnovers=await _turnover_calls(staff, config, now, routing),
        stalled=await _stalled(staff, config, now),
    )


async def _turnover_calls(
    staff: StaffAudience, config: TenantConfig | None, now: datetime, routing: FloorRouting
) -> list[TurnoverCallView]:
    """Номера доски, где следующий шаг за этим сотрудником (spec 0046 §7).

    Адресатов определяют права доски `turnovers.rights_for`: «Проверьте» — кому
    можно «Проверено» (хаускипинг), «Примите» — кому можно «Принять» (старший
    хаускипинга), замечание — кому можно «Отметить выезд» (ресепшен). Менеджера
    не зовут: права на всё у него — подстраховка, а сигнал звал бы его на каждый
    выезд отеля; как и с заявками, он слышит только застрявшее (spec 0045 §4).
    Выезд из номера, у этажа которого есть команда, «Проверьте» зовёт сначала
    её, а через срок `d` уборки от отметки — всех (spec 0053 §4).
    Доска выключена — пустой список без запроса в базу (spec 0046 §5).
    """
    if not turnovers.board_enabled() or staff.role_key is StaffRole.MANAGER:
        return []
    rights = turnovers.rights_for(staff)
    if not (rights.can_work or rights.can_accept or rights.can_report):
        return []
    open_turnovers = await housekeeping_api.list_open_turnovers(limit=turnovers.OPEN_LIMIT)
    remarks_after = now - REMARK_SIGNAL_WINDOW
    # Срок уборки нужен, только если этажи сужают «Проверьте».
    delay: timedelta | None = None
    if routing.staffed_floors and config is not None:
        delay = config.reminder_delay_for(HOUSEKEEPING_SERVICE_KEY)
    calls: list[TurnoverCallView] = []
    for turnover in reversed(open_turnovers):  # доска отдаёт давние сверху
        status = turnover.status
        if (
            rights.can_work
            and status is housekeeping_api.TurnoverStatus.REPORTED
            and (
                routing.calls_me_first(turnover.room_number)
                or step_by_time(now - turnover.reported_at, delay) is not LadderStep.SERVICE
            )
        ):
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


async def _unclaimed(
    staff: StaffAudience, config: TenantConfig | None, now: datetime, routing: FloorRouting
) -> list[UnclaimedRequestView]:
    """Невзятые заявки, которые зовут этого сотрудника (лестница spec 0045 §4,
    первая ступень заявки уборки по этажу — spec 0053 §4)."""
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
    orders = await room_service_api.orders_for_requests(requests)
    views = [
        _view(request, category_names, category_keys.get(request.category_id), config, now)
        for request in requests
    ]
    for request, view in zip(requests, views, strict=True):
        view.is_order = request.id in orders
    if not staff.sees_all_services:
        if not routing.staffed_floors:
            return views
        # Все заявки — её служб, а она сама в них активна: служба «укомплектована».
        return [
            view
            for request, view in zip(requests, views, strict=True)
            if _floor_lets_through(
                request,
                category_keys.get(request.category_id),
                _ladder(request, category_keys.get(request.category_id), config, now)[0],
                routing,
            )
        ]
    staffed = await staffed_service_keys(staff.tenant_id)
    addressed: list[UnclaimedRequestView] = []
    for request, view in zip(requests, views, strict=True):
        key = category_keys.get(request.category_id)
        step, by_time = _ladder(request, key, config, now, service_staffed=key in staffed)
        own = key in staff.service_keys and _floor_lets_through(request, key, step, routing)
        if own or step is LadderStep.RECEPTION:
            view.is_raised = by_time is LadderStep.RECEPTION
            addressed.append(view)
    return addressed


async def _stalled(
    staff: StaffAudience, config: TenantConfig | None, now: datetime
) -> list[StalledRequestView]:
    """Взятые и не закрытые дольше срока, которые зовут этого сотрудника (spec 0028 §8).

    Адресаты — кто видит заявку в очереди (`scope.allowed_category_ids`): служба
    заявки, ресепшен и менеджер. Этажи (spec 0053 §4) не сужают: к сроку «долго в
    работе» лестница давно зовёт всю службу. Отбор — тем же `queue.is_stalled`,
    что метка карточки (P-12): запрос берёт самый ранний срок отеля, правило
    категории решает по каждой заявке.
    """
    delay = config.min_stalled_delay() if config is not None else None
    if delay is None:
        return []
    categories = await requests_api.list_categories()
    requests = await requests_api.list_stalled_requests(
        claimed_before=now - delay,
        limit=_SIGNAL_LIMIT,
        category_ids=scope.allowed_category_ids(staff, categories),
    )
    names = {category.id: category.name for category in categories}
    keys = {category.id: category.key for category in categories}
    return [
        StalledRequestView(
            id=str(request.id),
            daily_number=request.daily_number,
            room_number=request.room_number,
            category_name=names.get(request.category_id, "—"),
            in_work_label=queue.age_label(request.claimed_at or request.created_at, now),
        )
        for request in requests
        if queue.is_stalled(request, keys.get(request.category_id), config, now)
    ]


def _ladder(
    request: requests_api.ServiceRequestRead,
    key: str | None,
    config: TenantConfig | None,
    now: datetime,
    *,
    service_staffed: bool = True,
) -> tuple[LadderStep, LadderStep]:
    """Ступень заявки и ступень по одному времени («Никто не взял» — по второй)."""
    delay = config.reminder_delay_for(key) if config is not None else None
    age = now - request.created_at
    step = ladder_step(
        age=age,
        delay=delay,
        is_urgent=request.is_urgent,
        is_escalation=request.origin is requests_api.ServiceRequestOrigin.ESCALATION,
        service_staffed=service_staffed,
    )
    return step, step_by_time(age, delay)


def _floor_lets_through(
    request: requests_api.ServiceRequestRead,
    key: str | None,
    step: LadderStep,
    routing: FloorRouting,
) -> bool:
    """Первая ступень заявки уборки по этажу зовёт только команду этажа (spec 0053 §4);
    со второй ступени, срочную и эскалацию — всю службу, как раньше."""
    if key != HOUSEKEEPING_SERVICE_KEY or step is not LadderStep.SERVICE:
        return True
    return routing.calls_me_first(request.room_number)


def _view(
    request: requests_api.ServiceRequestRead,
    category_names: dict[Any, str],
    category_key: str | None,
    config: TenantConfig | None,
    now: datetime,
) -> UnclaimedRequestView:
    return UnclaimedRequestView(
        id=str(request.id),
        category_id=str(request.category_id),
        daily_number=request.daily_number,
        room_number=request.room_number,
        category_name=category_names.get(request.category_id, "—"),
        is_urgent=request.is_urgent,
        is_overdue=queue.is_overdue(request, category_key, config, now),
        age_label=queue.age_label(request.created_at, now),
    )

"""Данные страницы очереди заявок (spec 0033 §5, PR D серии #48).

Собирает контекст шаблонов `queue.html` / `_queue_list.html`: лента открытых
(`new` + `in_progress`, новые сверху) или «закрытые за сегодня» (граница —
полночь отеля), фильтры-чипсы по категории и «мои». Фильтры — представление,
не право (spec 0033 §3.2): любой сотрудник видит всю очередь, срез — в памяти
(у пилота ~50–120 заявок в сутки, зоопарк SQL-параметров не окупается).

Сами действия («взять»/«готово»/«отменить») — `api_router.py`; маршруты
страницы — `router.py`. Здесь только чтение через `requests_api` (P-5, R-5).
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any, Final
from urllib.parse import urlencode

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

QUEUE_TAB_OPEN: Final = "open"
QUEUE_TAB_CLOSED: Final = "closed"

# Страховка ленты от неограниченного скана (канон list_unclaimed_requests):
# при ~50–120 заявках в сутки у пилота (spec 0033 §1) сюда упереться нельзя.
_QUEUE_LIMIT: Final = 200

# Русские подписи статусов для карточек (язык кабинета — русский, §7 FOUNDATION).
_STATUS_LABELS: Final[dict[requests_api.RequestStatus, str]] = {
    requests_api.RequestStatus.NEW: "новая",
    requests_api.RequestStatus.IN_PROGRESS: "в работе",
    requests_api.RequestStatus.DONE: "выполнена",
    requests_api.RequestStatus.CANCELLED: "отменена",
}


def parse_queue_tab(raw: str | None) -> str:
    """Вкладка из query-параметра; всё непонятное — «открытые» (не 422: ссылку
    с опечаткой мог прислать коллега, страница должна открыться)."""
    return QUEUE_TAB_CLOSED if raw == QUEUE_TAB_CLOSED else QUEUE_TAB_OPEN


def queue_path(tenant_slug: str, *, tab: str, category_key: str | None, mine: bool) -> str:
    """URL страницы очереди с фильтрами — чипсы-ссылки и fragment-эндпоинт."""
    params: dict[str, str] = {}
    if tab != QUEUE_TAB_OPEN:
        params["tab"] = tab
    if category_key:
        params["category"] = category_key
    if mine:
        params["mine"] = "1"
    query = f"?{urlencode(params)}" if params else ""
    return f"/staff/{tenant_slug}/requests{query}"


async def build_queue_context(
    staff: StaffContext, *, tab: str, category_key: str | None, mine: bool
) -> dict[str, Any]:
    """Контекст шаблона страницы очереди (вместе с чипсами и fragment-URL).

    Вызывается внутри контекста тенанта запроса (его ставит звено
    TenantResolver кабинета) — `requests_api` читает под RLS текущего тенанта.
    """
    categories = await requests_api.list_categories()
    if tab == QUEUE_TAB_CLOSED:
        closed_after = await _hotel_midnight_utc(staff)
        requests = await requests_api.list_requests_closed_since(
            closed_after=closed_after, limit=_QUEUE_LIMIT
        )
    else:
        requests = await requests_api.list_open_requests(limit=_QUEUE_LIMIT)

    if category_key:
        wanted = {category.id for category in categories if category.key == category_key}
        requests = [request for request in requests if request.category_id in wanted]
    if mine:
        requests = [request for request in requests if request.claimed_by_user_id == staff.user_id]

    category_names = {category.id: category.name for category in categories}
    now = utc_now()
    cards = [_card(request, category_names, now) for request in requests]

    def chip(
        label: str, *, tab_v: str, category_v: str | None, mine_v: bool, active: bool
    ) -> dict[str, Any]:
        return {
            "label": label,
            "href": queue_path(staff.tenant_slug, tab=tab_v, category_key=category_v, mine=mine_v),
            "active": active,
        }

    tabs = [
        chip(
            "Открытые",
            tab_v=QUEUE_TAB_OPEN,
            category_v=category_key,
            mine_v=mine,
            active=tab == QUEUE_TAB_OPEN,
        ),
        chip(
            "Закрытые за сегодня",
            tab_v=QUEUE_TAB_CLOSED,
            category_v=category_key,
            mine_v=mine,
            active=tab == QUEUE_TAB_CLOSED,
        ),
    ]
    filter_chips = [
        chip("Все", tab_v=tab, category_v=None, mine_v=mine, active=category_key is None),
        *(
            chip(
                category.name,
                tab_v=tab,
                category_v=category.key,
                mine_v=mine,
                active=category.key == category_key,
            )
            for category in categories
        ),
        chip("Мои", tab_v=tab, category_v=category_key, mine_v=not mine, active=mine),
    ]
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "cards": cards,
        "tabs": tabs,
        "filter_chips": filter_chips,
        "empty_text": (
            "Сегодня ещё ничего не закрывали."
            if tab == QUEUE_TAB_CLOSED
            else "Открытых заявок нет — отличная работа!"
        ),
        "fragment_path": queue_path(
            staff.tenant_slug, tab=tab, category_key=category_key, mine=mine
        ).replace("/requests", "/requests/fragment", 1),
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/requests",
    }


def _card(
    request: requests_api.ServiceRequestRead,
    category_names: dict[Any, str],
    now: datetime,
) -> dict[str, Any]:
    is_new = request.status is requests_api.RequestStatus.NEW
    is_in_progress = request.status is requests_api.RequestStatus.IN_PROGRESS
    return {
        "id": str(request.id),
        "daily_number": request.daily_number,
        "room_number": request.room_number,
        "category_name": category_names.get(request.category_id, "—"),
        "summary": request.summary,
        "details": request.details,
        "status": request.status.value,
        "status_label": _STATUS_LABELS.get(request.status, request.status.value),
        "age_label": _age_label(request.created_at, now),
        "claimed_by": request.claimed_by_display_name,
        "resolution_note": request.resolution_note,
        "can_claim": is_new,
        "can_complete": is_in_progress,
        "can_cancel": is_new or is_in_progress,
    }


def _age_label(created_at: datetime, now: datetime) -> str:
    """Возраст заявки для карточки: «5 мин», «3 ч», «2 дн» — по-русски, грубо."""
    minutes = max(0, int((now - created_at).total_seconds() // 60))
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    return f"{hours // 24} дн"


async def _hotel_midnight_utc(staff: StaffContext) -> datetime:
    """Полночь отеля «сегодня» в UTC — граница вкладки «закрытые за сегодня»
    (spec 0033 §5, Q3: граница суток — полночь отеля).

    Часовой пояс — из конфига тенанта; тенант без конфига (онбординг не
    завершён, служебный smoke-тенант) — деградация на UTC, а не отказ
    (канон `_hotel_service_day` модуля requests).
    """
    zone: tzinfo = UTC
    try:
        async with platform_session_scope() as session:
            config = await load_tenant_config(session, staff.tenant_id)
        zone = config.tzinfo
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("staff.queue_day_utc_fallback", error_code=error.code)
    local_midnight = utc_now().astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)

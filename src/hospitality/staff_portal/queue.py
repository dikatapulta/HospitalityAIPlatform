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
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
)
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

QUEUE_TAB_OPEN: Final = "open"
QUEUE_TAB_CLOSED: Final = "closed"

# Потолок дневного номера в плашке «заявка создана»: тот же, которым команды
# персонала в Telegram отсекают вставленный кусок номера карты (issue #203).
# Число здесь своё, а не импортированное: `channels/telegram` — сиблинг
# композиционного слоя, импортировать его кабинету нельзя (контракт 7).
_MAX_DAILY_NUMBER_DIGITS: Final = 5

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


def created_flash(raw: str | None) -> str | None:
    """Плашка после возврата с формы ручного приёма (spec 0035 §5/§9).

    Номер приходит query-параметром, то есть от кого угодно, — в текст он
    попадает только пройдя проверку «одни цифры, не длиннее пяти». Непонятный
    параметр даёт не 422, а просто отсутствие плашки: ссылку с опечаткой мог
    прислать коллега, а страница очереди обязана открыться (канон
    `parse_queue_tab`).
    """
    if raw is None or not raw.isdecimal() or not 1 <= len(raw) <= _MAX_DAILY_NUMBER_DIGITS:
        return None
    return f"Заявка #{raw} создана — уведомление ушло в чат службы."


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
    config = await _tenant_config(staff)
    if tab == QUEUE_TAB_CLOSED:
        requests = await requests_api.list_requests_closed_since(
            closed_after=_hotel_midnight_utc(config), limit=_QUEUE_LIMIT
        )
    else:
        requests = await requests_api.list_open_requests(limit=_QUEUE_LIMIT)

    if category_key:
        wanted = {category.id for category in categories if category.key == category_key}
        requests = [request for request in requests if request.category_id in wanted]
    if mine:
        requests = [request for request in requests if request.claimed_by_user_id == staff.user_id]

    category_names = {category.id: category.name for category in categories}
    category_keys = {category.id: category.key for category in categories}
    now = utc_now()
    cards = [_card(request, category_names, category_keys, config, now) for request in requests]

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
    category_keys: dict[Any, str],
    config: TenantConfig | None,
    now: datetime,
) -> dict[str, Any]:
    is_new = request.status is requests_api.RequestStatus.NEW
    is_in_progress = request.status is requests_api.RequestStatus.IN_PROGRESS
    # Бейдж показывает не статус, а состояние, которое важно смотрящему в
    # очередь: просроченная — всегда `new`, и знать, что она именно просрочена,
    # полезнее, чем что она «новая». Поэтому один бейдж, а не два рядом.
    overdue = _is_overdue(request, category_keys.get(request.category_id), config, now)
    return {
        "is_overdue": overdue,
        # Срочность — не статус, а свойство заявки (spec 0034 §5), поэтому она
        # отдельной меткой рядом с бейджем состояния, а не вместо него: «срочная
        # и просроченная» — худший случай, и он обязан читаться целиком.
        "is_urgent": request.is_urgent,
        "badge_label": "просрочена"
        if overdue
        else _STATUS_LABELS.get(request.status, request.status.value),
        "badge_tone": "overdue" if overdue else request.status.value,
        "id": str(request.id),
        "daily_number": request.daily_number,
        "room_number": request.room_number,
        "category_name": category_names.get(request.category_id, "—"),
        "summary": request.summary,
        "details": request.details,
        "age_label": _age_label(request.created_at, now),
        "claimed_by": request.claimed_by_display_name,
        "resolution_note": request.resolution_note,
        "can_claim": is_new,
        "can_complete": is_in_progress,
        "can_cancel": is_new or is_in_progress,
    }


def _is_overdue(
    request: requests_api.ServiceRequestRead,
    category_key: str | None,
    config: TenantConfig | None,
    now: datetime,
) -> bool:
    """Заявку не взяли дольше срока отеля — то же определение, что у напоминаний.

    Одно правило на два места (P-12): «невзятая» — это ровно `status = new`
    (spec 0028 §1; `in_progress`, висящий сутками, — отдельная задача #120), срок
    берётся тем же `TenantConfig.reminder_delay_for`, что и у воркера. Иначе
    подсветка в кабинете и напоминание в чат службы говорили бы о разных
    заявках, и персонал перестал бы верить обоим.

    Тенант без конфига (онбординг не завершён, smoke-тенант) и отель с
    выключенными напоминаниями (`None`) просрочки не имеют — деградация в
    «подсветки нет», а не отказ страницы.
    """
    if config is None or request.status is not requests_api.RequestStatus.NEW:
        return False
    delay = config.reminder_delay_for(category_key)
    return delay is not None and now - request.created_at >= delay


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


async def _tenant_config(staff: StaffContext) -> TenantConfig | None:
    """Конфиг отеля для страницы: часовой пояс и сроки напоминаний — одним чтением.

    Тенант без конфига (онбординг не завершён, служебный smoke-тенант) — `None`,
    а не отказ страницы: очередь обязана открываться и до онбординга (канон
    `_hotel_service_day` модуля requests). Читается один раз на запрос и служит
    обеим потребностям сразу — раньше поллинг закрытой вкладки ходил за тем же
    конфигом ради одного часового пояса.

    Остальные ошибки чтения пробрасываются НАМЕРЕННО (находка ревью #167):
    «конфиг в БД не проходит схему» (ERR-PLATFORM-006) — это дрейф данных, и он
    обязан быть громким. Цена решения выросла: конфиг читается на каждый запрос,
    поэтому такой дрейф теперь роняет всю страницу очереди, а не одну вкладку
    «закрытые за сегодня». Это лучше молчаливой очереди без просрочек, в которой
    персонал не увидит, что метка перестала работать.
    """
    try:
        async with platform_session_scope() as session:
            return await load_tenant_config(session, staff.tenant_id)
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        # Событие пишется и на каждый поллинг (15 с) — у тенанта без конфига это
        # заметный поток; схлопнется кэшем конфига (#168).
        logger.warning("staff.queue_tenant_config_missing", error_code=error.code)
        return None


def _hotel_midnight_utc(config: TenantConfig | None) -> datetime:
    """Полночь отеля «сегодня» в UTC — граница вкладки «закрытые за сегодня»
    (spec 0033 §5, Q3: граница суток — полночь отеля).

    Часовой пояс — из конфига тенанта; без конфига — деградация на UTC.
    """
    zone: tzinfo = config.tzinfo if config is not None else UTC
    local_midnight = utc_now().astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)

"""Данные страницы очереди заявок (spec 0033 §5, PR D серии #48).

Собирает контекст шаблонов `queue.html` / `_queue_list.html`: лента открытых
(`new` + `in_progress`, новые сверху), «закрытые за сегодня» или «за 7 дней»
(границы — полночь отеля), фильтры-чипсы по службе и «мои», поиск по комнате и
`#N` (`queue_filters.py`). Срез по службам — право, а не представление
(spec 0045 §5): сотрудник видит заявки своих служб, ресепшен и менеджер — все
(`scope.py`). Все срезы — служба, «мои», чипс, поиск — в SQL до потолка ленты
(#221): в памяти после среза вкладка службы при длинной общей очереди молча
показывала бы подмножество.

Сами действия («взять»/«готово»/«отменить», «вернуть в очередь»/«передать» —
spec 0042) — `api_router.py`; маршруты страницы — `router.py`. Здесь только
чтение через `requests_api` (P-5, R-5).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
)
from hospitality.platform.staff_auth import StaffAudience, StaffContext
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import act_requests, order_cards, scope
from hospitality.staff_portal import queue_filters as filters
from hospitality.staff_portal.ladder import LadderStep, step_by_time

logger = get_logger(module=__name__)

# Страховка ленты от неограниченного скана (канон list_unclaimed_requests):
# при ~50–120 заявках в сутки у пилота (spec 0033 §1) открытые сюда не упрутся,
# а «за 7 дней» без поиска — упрутся; лента говорит об этом (`truncated_text`)
# и отсылает к поиску, а не молчит (#221).
_QUEUE_LIMIT: Final = 200

# Русские подписи статусов для карточек (язык кабинета — русский, §7 FOUNDATION).
_STATUS_LABELS: Final[dict[requests_api.RequestStatus, str]] = {
    requests_api.RequestStatus.NEW: "новая",
    requests_api.RequestStatus.IN_PROGRESS: "в работе",
    requests_api.RequestStatus.DONE: "выполнена",
    requests_api.RequestStatus.CANCELLED: "отменена",
}
# Метка взятой и не закрытой дольше срока (spec 0028 §8): вместо «в работе».
STALLED_LABEL: Final = "долго в работе"


def created_flash(raw: str | None, *, foreign_service_name: str | None = None) -> str | None:
    """Плашка после возврата с формы ручного приёма (spec 0035 §5/§9).

    Номер приходит query-параметром, то есть от кого угодно, — в текст он
    попадает только пройдя проверку «одни цифры, не длиннее пяти». Непонятный
    параметр даёт не 422, а просто отсутствие плашки: ссылку с опечаткой мог
    прислать коллега, а страница очереди обязана открыться (канон
    `queue_filters.parse_queue_tab`).

    Заявка в службу, которой нет у сотрудника, в его очереди не появится —
    плашка называет службу (spec 0045 §3), иначе «создана», а в списке пусто.
    """
    if raw is None or not raw.isdecimal() or not 1 <= len(raw) <= filters.MAX_DAILY_NUMBER_DIGITS:
        return None
    if foreign_service_name is not None:
        return (
            f"Заявка #{raw} создана и ушла в службу «{foreign_service_name}» — "
            "в вашей очереди её не будет."
        )
    return f"Заявка #{raw} создана — уведомление ушло в чат службы."


async def build_queue_context(
    staff: StaffContext,
    *,
    tab: str,
    category_key: str | None,
    mine: bool,
    search: str | None = None,
    created: str | None = None,
    created_category_id: str | None = None,
) -> dict[str, Any]:
    """Контекст шаблона страницы очереди (вместе с чипсами и fragment-URL).

    Вызывается внутри контекста тенанта запроса (его ставит звено
    TenantResolver кабинета) — `requests_api` читает под RLS текущего тенанта.
    Чипс чужой службы сюда не доходит (роутер отвечает 403), а дошёл бы —
    дал бы пустую ленту: срез строится только из служб сотрудника.
    `created`/`created_category_id` — плашка после ручного приёма (у
    fragment-эндпоинта их нет: поллинг показывал бы её снова и снова).
    """
    categories = await requests_api.list_categories()
    allowed = scope.allowed_category_ids(staff, categories)
    visible = categories if allowed is None else [c for c in categories if c.id in allowed]
    category_ids = allowed
    if category_key:
        category_ids = frozenset(c.id for c in visible if c.key == category_key)
    claimed_by = staff.user_id if mine else None
    found = filters.parse_search(search)
    config = await tenant_config(staff)
    if tab == filters.QUEUE_TAB_CLOSED:
        requests = await requests_api.list_requests_closed_since(
            closed_after=hotel_midnight_utc(config),
            limit=_QUEUE_LIMIT,
            category_ids=category_ids,
            claimed_by_user_id=claimed_by,
            room_number=found.room_number,
            daily_number=found.daily_number,
        )
    elif tab == filters.QUEUE_TAB_WEEK:
        requests = await requests_api.list_requests_created_since(
            created_after=hotel_midnight_utc(config, days_back=filters.WEEK_DAYS - 1),
            limit=_QUEUE_LIMIT,
            category_ids=category_ids,
            claimed_by_user_id=claimed_by,
            room_number=found.room_number,
            daily_number=found.daily_number,
        )
    else:
        requests = await requests_api.list_open_requests(
            limit=_QUEUE_LIMIT,
            category_ids=category_ids,
            claimed_by_user_id=claimed_by,
            room_number=found.room_number,
            daily_number=found.daily_number,
        )

    category_names = {category.id: category.name for category in categories}
    category_keys = {category.id: category.key for category in categories}
    # Заказ в номер — заявка со своей карточкой (spec 0038 §5): один запрос на ленту.
    orders = await room_service_api.orders_for_requests(requests)
    now = utc_now()
    cards = [
        _with_order(
            _card(request, category_names, category_keys, config, now, staff.tenant_slug),
            orders.get(request.id),
            request,
            config,
        )
        for request in requests
    ]

    def path(*, tab_v: str = tab, category_v: str | None, mine_v: bool, search_v: str) -> str:
        return filters.queue_path(
            staff.tenant_slug, tab=tab_v, category_key=category_v, mine=mine_v, search=search_v
        )

    def chip(
        label: str, *, tab_v: str, category_v: str | None, mine_v: bool, active: bool
    ) -> dict[str, Any]:
        # Чипсы и вкладки поиск не сбрасывают: «1207» ищут и в открытых, и за 7 дней.
        href = path(tab_v=tab_v, category_v=category_v, mine_v=mine_v, search_v=found.text)
        return {"label": label, "href": href, "active": active}

    tabs = [
        chip(label, tab_v=tab_v, category_v=category_key, mine_v=mine, active=tab == tab_v)
        for label, tab_v in (
            ("Открытые", filters.QUEUE_TAB_OPEN),
            ("Закрытые за сегодня", filters.QUEUE_TAB_CLOSED),
            ("За 7 дней", filters.QUEUE_TAB_WEEK),
        )
    ]
    # Чипсы — только службы сотрудника; у кого служба одна, чипс повторял бы «Все».
    service_chips = [
        chip(
            category.name,
            tab_v=tab,
            category_v=category.key,
            mine_v=mine,
            active=category.key == category_key,
        )
        for category in visible
    ]
    filter_chips = [
        chip("Все", tab_v=tab, category_v=None, mine_v=mine, active=category_key is None),
        *(service_chips if len(service_chips) > 1 or category_key else []),
        chip("Мои", tab_v=tab, category_v=category_key, mine_v=not mine, active=mine),
    ]
    no_services = scope.has_no_services(staff)
    searching = bool(found.text) and not no_services
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "cards": cards,
        "tabs": tabs,
        "filter_chips": filter_chips,
        "empty_text": scope.NO_SERVICES_TEXT if no_services else filters.empty_text(tab, found),
        # Только для пустой ленты под поиском: подсказка про `#` и тот же поиск за 7 дней.
        "number_hint": filters.number_hint(found) if searching else None,
        "week_search_href": (
            path(
                tab_v=filters.QUEUE_TAB_WEEK,
                category_v=category_key,
                mine_v=mine,
                search_v=found.text,
            )
            if searching and tab != filters.QUEUE_TAB_WEEK
            else None
        ),
        "truncated_text": (
            f"Показаны {_QUEUE_LIMIT} самых новых заявок — более старые найдёт поиск "
            "по комнате или #номеру."
            if len(requests) >= _QUEUE_LIMIT
            else None
        ),
        # Строка поиска: скрытые поля держат остальные фильтры, «Сбросить» — без `q`.
        "search_text": found.text,
        "search_action": f"/staff/{staff.tenant_slug}/requests",
        "search_fields": filters.queue_params(tab=tab, category_key=category_key, mine=mine),
        "search_reset_href": (
            path(category_v=category_key, mine_v=mine, search_v="") if found.text else None
        ),
        "flash": created_flash(
            created,
            foreign_service_name=_foreign_service_name(created_category_id, categories, allowed),
        ),
        "fragment_path": path(category_v=category_key, mine_v=mine, search_v=found.text).replace(
            "/requests", "/requests/fragment", 1
        ),
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/requests",
        "orders_endpoint": f"/staff/{staff.tenant_slug}/api/orders",
    }


def _with_order(
    card: dict[str, Any],
    order: room_service_api.OrderRead | None,
    request: requests_api.ServiceRequestRead,
    config: TenantConfig | None,
) -> dict[str, Any]:
    """Заказу — своя карточка (`order_cards.py`): шаблон по `order` рисует её
    кнопки вместо общих действий заявки."""
    if order is None:
        return card | {"order": None}
    fields = order_cards.order_card(order, request, config, is_overdue=card["is_overdue"])
    return card | {"order": fields, "badge_label": fields["badge_label"]}


def _foreign_service_name(
    raw_category_id: str | None,
    categories: list[requests_api.RequestCategoryRead],
    allowed: frozenset[Any] | None,
) -> str | None:
    """Имя службы только что созданной заявки, если её нет у сотрудника.

    Id приходит query-параметром — непонятный или чужой отелю молча даёт
    обычную плашку, как непонятный номер (`created_flash`).
    """
    if raw_category_id is None or allowed is None:
        return None
    for category in categories:
        if str(category.id) == raw_category_id and category.id not in allowed:
            return category.name
    return None


async def service_names(staff: StaffContext) -> list[str]:
    """Названия служб сотрудника — для страницы «нет доступа» к чужой службе."""
    return [
        category.name
        for category in await requests_api.list_categories()
        if category.key in staff.service_keys
    ]


def _card(
    request: requests_api.ServiceRequestRead,
    category_names: dict[Any, str],
    category_keys: dict[Any, str],
    config: TenantConfig | None,
    now: datetime,
    tenant_slug: str,
) -> dict[str, Any]:
    is_new = request.status is requests_api.RequestStatus.NEW
    is_in_progress = request.status is requests_api.RequestStatus.IN_PROGRESS
    # Передать можно в любую службу отеля, кроме текущей (spec 0042 §2.4);
    # порядок — как у чипсов фильтра (`list_categories` сортирует по ключу).
    transfer_options = [
        {"id": str(category_id), "name": name}
        for category_id, name in category_names.items()
        if category_id != request.category_id
    ]
    # Бейдж показывает не статус, а состояние, которое важно смотрящему в
    # очередь: просроченная — всегда `new`, и знать, что она именно просрочена,
    # полезнее, чем что она «новая». Поэтому один бейдж, а не два рядом.
    overdue = is_overdue(request, category_keys.get(request.category_id), config, now)
    stalled = is_stalled(request, category_keys.get(request.category_id), config, now)
    return {
        "is_overdue": overdue,
        "is_stalled": stalled,
        # Срочность — не статус, а свойство заявки (spec 0034 §5), поэтому она
        # отдельной меткой рядом с бейджем состояния, а не вместо него: «срочная
        # и просроченная» — худший случай, и он обязан читаться целиком.
        "is_urgent": request.is_urgent,
        "badge_label": "просрочена"
        if overdue
        else STALLED_LABEL
        if stalled
        else _STATUS_LABELS.get(request.status, request.status.value),
        "badge_tone": "overdue" if overdue or stalled else request.status.value,
        "id": str(request.id),
        "daily_number": request.daily_number,
        "room_number": request.room_number,
        "category_name": category_names.get(request.category_id, "—"),
        "summary": request.summary,
        "details": request.details,
        "age_label": age_label(request.created_at, now),
        "claimed_by": request.claimed_by_display_name,
        "resolution_note": request.resolution_note,
        "can_claim": is_new,
        "can_complete": is_in_progress,
        "can_requeue": is_in_progress,
        "can_transfer": (is_new or is_in_progress) and bool(transfer_options),
        "transfer_options": transfer_options,
        "can_cancel": is_new or is_in_progress,
        # Запрос АВР (spec 0049 §6): реквизиты — на своей странице, строка — по якорю.
        "act_request_href": (
            f"{act_requests.page_path(tenant_slug)}#request-{request.id}"
            if act_requests.is_act_request(request, category_keys.get(request.category_id))
            else None
        ),
    }


def is_overdue(
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
    # Та же граница, что у второй ступени лестницы (spec 0045 §4) — одна функция.
    delay = config.reminder_delay_for(category_key)
    return step_by_time(now - request.created_at, delay) >= LadderStep.OVERDUE


def is_stalled(
    request: requests_api.ServiceRequestRead,
    category_key: str | None,
    config: TenantConfig | None,
    now: datetime,
) -> bool:
    """Заявку взяли и не закрыли дольше срока отеля (spec 0028 §8, #120).

    Пара `is_overdue` и то же правило, что у напоминания в чат службы
    (`TenantConfig.stalled_delay_for`, отсчёт от взятия; у старых строк без
    `claimed_at` — от создания). Срок по умолчанию выключен — метки нет.
    """
    if config is None or request.status is not requests_api.RequestStatus.IN_PROGRESS:
        return False
    delay = config.stalled_delay_for(category_key)
    return delay is not None and now - (request.claimed_at or request.created_at) >= delay


def age_label(created_at: datetime, now: datetime) -> str:
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


async def tenant_config(staff: StaffAudience) -> TenantConfig | None:
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


def hotel_midnight_utc(config: TenantConfig | None, *, days_back: int = 0) -> datetime:
    """Полночь отеля «сегодня» (или `days_back` дней назад) в UTC — граница
    вкладок «закрытые за сегодня» и «за 7 дней» (spec 0033 §5, Q3: граница
    суток — полночь отеля).

    Часовой пояс — из конфига тенанта; без конфига — деградация на UTC. Дни
    отнимаются от местной даты, а не от момента в UTC: смена поясного времени
    внутри недели не уводит границу с полуночи.
    """
    zone: tzinfo = config.tzinfo if config is not None else UTC
    local_day = utc_now().astimezone(zone).date() - timedelta(days=days_back)
    return datetime.combine(local_day, time.min, tzinfo=zone).astimezone(UTC)

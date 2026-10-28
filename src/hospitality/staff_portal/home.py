"""Данные главной кабинета: приветствие, плитки с живыми числами, «Требует
внимания» и быстрые действия (issue #454; вид — макет staff-dashboard-v2, #462).

Числа считают не здесь, а владельцы данных через свои `api.py` (P-5, R-5):
открытые заявки — `requests_api.list_open_requests` (тот же срез по службам
сотрудника и потолок, что у ленты очереди, spec 0045 §5), просрочка —
`queue.is_overdue` (одно правило с карточкой очереди и сигналом кабинета,
P-12), заселение — `guests_api.list_active_stays`.
Здесь только раскладка по плиткам и граница местного дня отеля.

Видимость — та же мини-матрица docs/RBAC.md, что у бокового меню: плитки и
быстрые действия берутся из `shell.nav_items` (P-12). Плитка числа есть только
у того, кому открыт её раздел: горничной незачем считать заселения, и её
страница не читает Stay вовсе.

Отель без конфига (онбординг не завершён) открывает главную без ошибки:
часы и день — по UTC (канон `checkin.hotel_zone`), но приветствие без времени
суток и без даты — UTC-утро у отеля в Алматы было бы враньём.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import month_day_ru
from hospitality.staff_portal import demo_orders, queue, scope, shell

# Потолок среза открытых заявок — тот же, что у ленты очереди: число на плитке
# обязано совпадать с тем, что сотрудник увидит, открыв очередь.
_OPEN_REQUESTS_LIMIT: Final = 200
# Строк в «Требует внимания» — как в макете: дольше всех ждущие, не вся очередь.
_ATTENTION_ROWS: Final = 3

_WEEKDAYS: Final = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

# Быстрые действия — разделы меню без чисел на плитках (их и так видно выше).
_QUICK_KEYS: Final = (
    "handover",
    "catalog",
    "act_requests",
    "mentor",
    "summary",
    "knowledge",
    "changes",
    "team",
)


def greeting(local_hour: int | None) -> str:
    """Приветствие по местному часу отеля; без часа (нет конфига) — нейтральное."""
    if local_hour is None:
        return "Здравствуйте"
    if 5 <= local_hour < 12:
        return "Доброе утро"
    if 12 <= local_hour < 18:
        return "Добрый день"
    if 18 <= local_hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def date_label(day: date) -> str:
    """«Понедельник, 19 октября» — без локали процесса (её в контейнере нет)."""
    return f"{_WEEKDAYS[day.weekday()]}, {month_day_ru(day)}"


def first_name(display_name: str) -> str:
    """Первое слово имени: «Доброе утро, Аружан», а не «…, Аружан Менеджер»."""
    words = display_name.split()
    return words[0] if words else display_name


async def build_home_context(staff: StaffContext, *, several_hotels: bool) -> dict[str, Any]:
    """Контекст шаблона `home.html`.

    Вызывается внутри контекста тенанта запроса (звено TenantResolver) —
    `requests_api` и `guests_api` читают под RLS текущего тенанта.
    """
    config = await queue.tenant_config(staff)
    now = utc_now()
    local_now = now.astimezone(config.tzinfo) if config is not None else now.astimezone(UTC)
    nav = {item["key"]: item for item in shell.nav_items(staff, config)}

    # Категории нужны раньше заявок, только если очередь режется по службам.
    categories = None if staff.sees_all_services else await requests_api.list_categories()
    allowed = None if categories is None else scope.allowed_category_ids(staff, categories)
    requests = await requests_api.list_open_requests(
        limit=_OPEN_REQUESTS_LIMIT, category_ids=allowed
    )
    overdue_ids: set[Any] = set()
    if requests:
        if categories is None:
            categories = await requests_api.list_categories()
        category_keys = {category.id: category.key for category in categories}
        overdue_ids = {
            request.id
            for request in requests
            if queue.is_overdue(request, category_keys.get(request.category_id), config, now)
        }

    metric_tiles = [_queue_tile(nav["requests"]["href"], requests, len(overdue_ids))]
    if "checkin" in nav:
        metric_tiles.append(await _checkin_tile(nav["checkin"]["href"], local_now))
    if "orders" in nav:
        metric_tiles.append(_orders_tile(nav["orders"]["href"]))

    return {
        "tenant_name": staff.tenant_name,
        "greeting": greeting(local_now.hour if config is not None else None),
        "first_name": first_name(staff.display_name),
        "date_label": date_label(local_now.date()) if config is not None else None,
        "handover_href": nav["handover"]["href"] if "handover" in nav else None,
        "metric_tiles": metric_tiles,
        "attention": _attention(requests, overdue_ids, now),
        "no_services_text": scope.NO_SERVICES_TEXT if scope.has_no_services(staff) else None,
        "queue_href": nav["requests"]["href"],
        "quick_actions": [
            {"title": "Новая заявка", "href": f"{nav['requests']['href']}/new", "icon": "plus"},
            *(nav[key] for key in _QUICK_KEYS if key in nav),
        ],
        "several_hotels": several_hotels,
    }


def _queue_tile(
    href: str, requests: list[requests_api.ServiceRequestRead], overdue_count: int
) -> dict[str, Any]:
    """«Очередь заявок»: открыто сейчас; новых, в работе и — если есть — просрочено.

    «Просрочено: 0» не показывается: у отеля с выключенными напоминаниями
    (или без конфига) просрочки не бывает по определению, и ноль читался бы
    как «сроки соблюдены» (тот же довод, что прочерк на сводке дня).
    """
    new_count = sum(1 for request in requests if request.status is requests_api.RequestStatus.NEW)
    chips = [
        {"label": "Новых", "value": new_count, "tone": "new"},
        {"label": "В работе", "value": len(requests) - new_count, "tone": "progress"},
    ]
    if overdue_count:
        chips.append({"label": "Просрочено", "value": overdue_count, "tone": "overdue"})
    return {
        "key": "queue",
        "title": "Очередь заявок",
        "href": href,
        "icon": "inbox",
        "value": len(requests),
        "caption": "открыто сейчас",
        "chips": chips,
        "accent": True,
        "alert": overdue_count > 0,
        "demo": False,
    }


async def _checkin_tile(href: str, local_now: datetime) -> dict[str, Any]:
    """«Заселение»: заселено и выезжают сегодня — по местному дню отеля.

    Оба числа — по живущим сейчас (`checked_in`): выехавший уже не «выезжает
    сегодня»; гость, заселённый и выселенный в один день, в «заселено» не
    попадает — для гостиницы без дневных номеров это пренебрежимо, а лишнего
    запроса к модулю guests ради него не заводим. «Ожидают заезда» из макета
    нет: броней у нас нет, с Opera не связаны.
    """
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    stays = await guests_api.list_active_stays()
    checked_in_today = sum(1 for stay in stays if day_start <= stay.check_in_at < day_end)
    leaving_today = sum(1 for stay in stays if day_start <= stay.check_out_at < day_end)
    return {
        "key": "checkin",
        "title": "Заселение",
        "href": href,
        "icon": "key",
        "value": checked_in_today,
        "caption": "заселено сегодня",
        "chips": [{"label": "Выезжают сегодня", "value": leaving_today, "tone": "muted"}],
        "accent": False,
        "alert": False,
        "demo": False,
    }


def _orders_tile(href: str) -> dict[str, Any]:
    """«Заказы в номер» (демо): числа выдуманных заказов демо-страницы."""
    counts = demo_orders.stage_counts()
    active = counts["new"] + counts["accepted"] + counts["paid"]
    return {
        "key": "orders",
        "title": "Заказы в номер",
        "href": href,
        "icon": "tray",
        "value": active,
        "caption": "активных заказов",
        "chips": [
            {"label": "Новых", "value": counts["new"], "tone": "new"},
            {"label": "Готовятся", "value": counts["accepted"], "tone": "progress"},
            {"label": "Оплачено", "value": counts["paid"], "tone": "muted"},
        ],
        "accent": False,
        "alert": False,
        "demo": True,
    }


def _attention(
    requests: list[requests_api.ServiceRequestRead], overdue_ids: set[Any], now: datetime
) -> list[dict[str, Any]]:
    """«Требует внимания»: заявки, которые ждут дольше всех.

    Сначала просроченные, затем невзятые, затем взятые — внутри каждой группы
    старшие первыми. Возраст невзятой — от создания, взятой — от взятия: «в
    работе 5 мин» говорит о том, сколько её делают, а не сколько она живёт.
    """
    new = requests_api.RequestStatus.NEW

    def rank(request: requests_api.ServiceRequestRead) -> tuple[int, datetime]:
        group = 0 if request.id in overdue_ids else 1 if request.status is new else 2
        return group, request.created_at

    rows = []
    for request in sorted(requests, key=rank)[:_ATTENTION_ROWS]:
        if request.status is new:
            tone = "overdue" if request.id in overdue_ids else "new"
            label = "Просрочена" if tone == "overdue" else "Новая"
            meta = _since("ждёт", request.created_at, now)
        else:
            tone, label = "progress", "В работе"
            since = request.claimed_at or request.created_at
            who = request.claimed_by_display_name
            meta = f"{who + ' · ' if who else ''}{_since('в работе', since, now)}"
        number = f"#{request.daily_number} · " if request.daily_number is not None else ""
        rows.append(
            {
                "room": request.room_number or "—",
                "summary": request.summary,
                "meta": f"{number}{meta}",
                "tone": tone,
                "label": label,
            }
        )
    return rows


def _since(prefix: str, moment: datetime, now: datetime) -> str:
    """«ждёт 12 мин»; свежая — «только что», без «ждёт только что»."""
    age = queue.age_label(moment, now)
    return age if age == "только что" else f"{prefix} {age}"

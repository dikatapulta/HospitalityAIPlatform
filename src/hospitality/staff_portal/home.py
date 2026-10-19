"""Данные главной кабинета: приветствие, плитки разделов с живыми числами (issue #454).

Числа считают не здесь, а владельцы данных через свои `api.py` (P-5, R-5):
открытые заявки — `requests_api.list_open_requests` (тот же срез и потолок,
что у ленты очереди), просрочка — `queue.is_overdue` (одно правило с карточкой
очереди и сигналом кабинета, P-12), заселение — `guests_api.list_active_stays`.
Здесь только раскладка по плиткам и граница местного дня отеля.

Видимость разделов — мини-матрица docs/RBAC.md: очередь — всем, заселение и
«Передача смены» — ресепшену и менеджеру, сводка, журнал и сотрудники —
менеджеру; демо-разделы — только при своих выключателях, с пометкой «демо».
Плитка числа показывает только тем, кому открыт её раздел: горничной незачем
считать заселения, и её страница не читает Stay вовсе.

Отель без конфига (онбординг не завершён) открывает главную без ошибки:
часы и день — по UTC (канон `checkin.hotel_zone`), но приветствие без времени
суток и без даты — UTC-утро у отеля в Алматы было бы враньём.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import month_day_ru
from hospitality.staff_portal import queue, team

# Потолок среза открытых заявок — тот же, что у ленты очереди: число на плитке
# обязано совпадать с тем, что сотрудник увидит, открыв очередь.
_OPEN_REQUESTS_LIMIT: Final = 200

_WEEKDAYS: Final = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

_RECEPTION_ROLES: Final = (StaffRole.RECEPTIONIST, StaffRole.MANAGER)


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
    slug = staff.tenant_slug
    is_reception = staff.role_key in _RECEPTION_ROLES
    is_manager = staff.role_key is StaffRole.MANAGER
    settings = get_settings()

    metric_tiles = [await _queue_tile(slug, config, now)]
    if is_reception:
        metric_tiles.append(await _checkin_tile(slug, local_now))

    sections: list[dict[str, Any]] = []
    if is_reception and settings.enable_demo_handover:
        sections.append(
            _section(
                slug,
                "handover",
                "Передача смены",
                "Касса одним кликом и общий лист передачи",
                icon="handover",
                demo=True,
            )
        )
    if is_manager:
        sections.append(
            _section(slug, "summary", "Сводка дня", "Числа за сегодня и вчера", icon="chart")
        )
        sections.append(
            _section(
                slug,
                "changes",
                "Журнал изменений",
                "Кто что сделал с заявками и заселением",
                icon="history",
            )
        )
        sections.append(
            _section(slug, "team", "Сотрудники", "Состав, приглашения, роли", icon="people")
        )
    if settings.enable_demo_orders:
        sections.append(
            _section(slug, "orders", "Заказы в номер", "Ресторан и магазин", icon="tray", demo=True)
        )

    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "role_label": team.role_label(staff.role_key),
        "greeting": greeting(local_now.hour if config is not None else None),
        "first_name": first_name(staff.display_name),
        "date_label": date_label(local_now.date()) if config is not None else None,
        "metric_tiles": metric_tiles,
        "sections": sections,
        "several_hotels": several_hotels,
    }


def _section(
    slug: str, path: str, title: str, note: str, *, icon: str, demo: bool = False
) -> dict[str, Any]:
    return {
        "title": title,
        "note": note,
        "href": f"/staff/{slug}/{path}",
        "icon": icon,
        "demo": demo,
    }


async def _queue_tile(slug: str, config: TenantConfig | None, now: datetime) -> dict[str, Any]:
    """«Очередь заявок»: открыто сейчас; новых, в работе и — если есть — просрочено.

    «Просрочено: 0» не показывается: у отеля с выключенными напоминаниями
    (или без конфига) просрочки не бывает по определению, и ноль читался бы
    как «сроки соблюдены» (тот же довод, что прочерк на сводке дня).
    """
    requests = await requests_api.list_open_requests(limit=_OPEN_REQUESTS_LIMIT)
    new_count = sum(1 for request in requests if request.status is requests_api.RequestStatus.NEW)
    overdue_count = 0
    if requests:
        categories = await requests_api.list_categories()
        category_keys = {category.id: category.key for category in categories}
        overdue_count = sum(
            1
            for request in requests
            if queue.is_overdue(request, category_keys.get(request.category_id), config, now)
        )
    chips = [
        {"label": "Новых", "value": new_count, "tone": "new"},
        {"label": "В работе", "value": len(requests) - new_count, "tone": "progress"},
    ]
    if overdue_count:
        chips.append({"label": "Просрочено", "value": overdue_count, "tone": "overdue"})
    return {
        "key": "queue",
        "title": "Очередь заявок",
        "href": f"/staff/{slug}/requests",
        "icon": "inbox",
        "value": len(requests),
        "caption": "открыто сейчас",
        "chips": chips,
        "alert": overdue_count > 0,
    }


async def _checkin_tile(slug: str, local_now: datetime) -> dict[str, Any]:
    """«Заселение»: заселено и выезжают сегодня — по местному дню отеля.

    Оба числа — по живущим сейчас (`checked_in`): выехавший уже не «выезжает
    сегодня»; гость, заселённый и выселенный в один день, в «заселено» не
    попадает — для гостиницы без дневных номеров это пренебрежимо, а лишнего
    запроса к модулю guests ради него не заводим.
    """
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    stays = await guests_api.list_active_stays()
    checked_in_today = sum(1 for stay in stays if day_start <= stay.check_in_at < day_end)
    leaving_today = sum(1 for stay in stays if day_start <= stay.check_out_at < day_end)
    return {
        "key": "checkin",
        "title": "Заселение",
        "href": f"/staff/{slug}/checkin",
        "icon": "key",
        "value": checked_in_today,
        "caption": "заселено сегодня",
        "chips": [{"label": "Выезжают сегодня", "value": leaving_today, "tone": "muted"}],
        "alert": False,
    }

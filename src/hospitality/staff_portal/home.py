"""Данные главной кабинета v3 (issue #635, PR 2; макет основателя 02.11, до него
— staff-dashboard-v2, #462): приветствие со строкой-итогом, карточки с живыми
числами, «Требует внимания» вкладками, быстрые действия и «К передаче смены».

Числа считают не здесь, а владельцы данных через свои `api.py` (P-5, R-5):
открытые заявки — `requests_api.list_open_requests` (тот же срез по службам
сотрудника и потолок, что у ленты очереди, spec 0045 §5), просрочка —
`queue.is_overdue` и «долго в работе» — `queue.is_stalled` (одно правило с
карточкой очереди и сигналом кабинета, P-12), заезды и выезды — `guests_api.list_active_stays`,
выезды и уборка — срезы самой доски (`turnovers.board_counts`).
Здесь только раскладка и граница местного дня отеля.

«Просрочено» — только невзятые сверх срока отеля, как метка «просрочена» в
очереди; «долго в работе» видно меткой строки, но в это число не идёт. Отель
без срока ответа (напоминания выключены или конфига нет) просрочки не имеет
по определению: карточки «Просрочено» и вкладки «Просроченные» у него нет —
ноль читался бы как «сроки соблюдены».

Видимость — та же мини-матрица docs/RBAC.md, что у бокового меню: карточки и
быстрые действия берутся из `shell.nav_items` (P-12). Карточка числа есть только
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
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import month_day_ru, plural
from hospitality.staff_portal import queue, queue_filters, scope, shell, turnovers

# Потолок среза открытых заявок — тот же, что у ленты очереди: число на карточке
# обязано совпадать с тем, что сотрудник увидит, открыв очередь.
_OPEN_REQUESTS_LIMIT: Final = 200
# Строк во вкладке «Требует внимания» — дольше всех ждущие, не вся очередь:
# остальное — ссылкой «Ещё N — в очереди».
ATTENTION_ROWS: Final = 8

_WEEKDAYS: Final = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

# Быстрые действия — разделы меню без своих карточек (их и так видно выше).
_QUICK_KEYS: Final = (
    "floors",
    "handover",
    "catalog",
    "act_requests",
    "mentor",
    "mentor_link",
    "codex_link",
    "model_3d_link",
    "summary",
    "knowledge",
    "changes",
    "stats",
    "team",
)

# Плашка менеджеру: у отеля не выбрана служба эскалаций (#786, spec 0039 §2.3) —
# гость, которому нужен человек, виден только в Telegram. Текст — показ основателю
# до кода, docs/founder-review/.
ESCALATION_NOTICE_TITLE: Final = "Не выбрана служба для гостей, которым нужен человек."
ESCALATION_NOTICE_TEXT: Final = (
    "Такие обращения не попадут в кабинет — только в Telegram. "
    "Обратитесь к администратору платформы."
)

# Вкладки «Требует внимания»: ключ, надпись, текст пустой вкладки.
_TABS: Final = (
    ("late", "Просроченные", "Просроченных заявок нет"),
    ("new", "Новые", "Новых заявок нет"),
    ("progress", "В работе", "Заявок в работе нет"),
    ("all", "Все", "Открытых заявок нет"),
)

# Цвет точки легенды «Выезды и уборка» по плитке доски: срочный номер — красным
# (spec 0062 §2); новый выезд — акцентом, как новая заявка; уборка идёт —
# фиолетовым, как заявка в работе.
_TURNOVER_TONES: Final = {
    turnovers.URGENT_KEY: "urgent",
    turnovers.TurnoverStatus.REPORTED.value: "new",
    turnovers.TurnoverStatus.CLEANING.value: "progress",
}


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


def _requests_word(count: int) -> str:
    return plural(count, "заявка", "заявки", "заявок")


async def build_home_context(staff: StaffContext) -> dict[str, Any]:
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
    if requests and categories is None:
        categories = await requests_api.list_categories()
    keys = {category.id: category.key for category in categories or ()}
    names = {category.id: category.name for category in categories or ()}
    overdue_ids = {
        r.id for r in requests if queue.is_overdue(r, keys.get(r.category_id), config, now)
    }
    stalled_ids = {
        r.id for r in requests if queue.is_stalled(r, keys.get(r.category_id), config, now)
    }
    # Срок ответа у отеля задан хотя бы для одной службы — просрочка бывает.
    tracked = config is not None and config.min_reminder_delay() is not None
    new_count = sum(1 for r in requests if r.status is requests_api.RequestStatus.NEW)
    no_services = scope.has_no_services(staff)

    oldest_late = min(
        (r for r in requests if r.id in overdue_ids), key=lambda r: r.created_at, default=None
    )
    kpis = [_late_kpi(len(overdue_ids), oldest_late, now)] if tracked else []
    kpis.append(_open_kpi(len(requests), new_count))
    if "checkin" in nav:
        kpis.append(await _today_kpi(nav["checkin"]["href"], local_now))
    if "turnovers" in nav:
        kpis.append(await _turnovers_kpi(nav["turnovers"]["href"], config))

    queue_href = nav["requests"]["href"]
    tabs, rows = _attention(
        requests,
        overdue_ids=overdue_ids,
        stalled_ids=stalled_ids,
        tracked=tracked,
        names=names,
        now=now,
        tenant_slug=staff.tenant_slug,
    )
    return {
        "tenant_name": staff.tenant_name,
        "greeting": greeting(local_now.hour if config is not None else None),
        "first_name": first_name(staff.display_name),
        "date_label": date_label(local_now.date()) if config is not None else None,
        "summary": None if no_services else _summary(tracked, len(overdue_ids), len(requests)),
        "escalation_notice": _escalation_notice(staff, config),
        "handover_href": nav["handover"]["href"] if "handover" in nav else None,
        "new_request_href": f"{queue_href}/new",
        "kpis": kpis,
        "tabs": tabs,
        "active_tab": next((tab["key"] for tab in tabs if tab["count"]), "all"),
        "rows": rows,
        "no_services_text": scope.NO_SERVICES_TEXT if no_services else None,
        "queue_href": queue_href,
        "claim_endpoint": f"/staff/{staff.tenant_slug}/api/requests",
        "quick_actions": [
            {"title": "Новая заявка", "href": f"{queue_href}/new", "icon": "plus", "accent": True},
            *(nav[key] for key in _QUICK_KEYS if key in nav),
        ],
        "handover_numbers": (
            {
                "open": len(requests),
                "late": len(overdue_ids) if tracked else None,
                "free": new_count,
            }
            if "handover" in nav
            else None
        ),
    }


def _escalation_notice(staff: StaffContext, config: TenantConfig | None) -> dict[str, str] | None:
    """Плашка «не выбрана служба эскалаций» — только менеджеру (#786).

    Чинит дыру администратор платформы онбордингом, но узнать о ней должен
    тот, кто отвечает за отель: остальным сотрудникам плашка — шум без действия.
    Отель без конфига плашку не получает: у него не работает бот целиком.
    """
    if staff.role_key is not StaffRole.MANAGER or config is None:
        return None
    if not config.escalation_choice_missing:
        return None
    return {"title": ESCALATION_NOTICE_TITLE, "text": ESCALATION_NOTICE_TEXT}


def _summary(tracked: bool, late_count: int, open_count: int) -> dict[str, str]:
    """Строка-итог под приветствием: начать с просроченных или «всё в срок»."""
    if tracked and late_count:
        word = plural(
            late_count, "просроченная заявка", "просроченные заявки", "просроченных заявок"
        )
        return {"tone": "warn", "icon": "warn", "text": f"{late_count} {word}. Начните с них."}
    if not open_count:
        return {"tone": "ok", "icon": "check", "text": "Открытых заявок нет — очередь пуста."}
    if tracked:
        return {"tone": "ok", "icon": "check", "text": "Просроченных заявок нет. Хорошая смена."}
    word = plural(open_count, "открытая заявка", "открытые заявки", "открытых заявок")
    return {"tone": "muted", "icon": "inbox", "text": f"{open_count} {word}."}


def _late_kpi(
    count: int, oldest: requests_api.ServiceRequestRead | None, now: datetime
) -> dict[str, Any]:
    """«Просрочено»: невзятые сверх срока; подпись — кто ждёт дольше всех."""
    if oldest is None:
        foot = "Все заявки укладываются в срок"
    else:
        who = f"№{oldest.room_number}" if oldest.room_number else "заявка"
        foot = f"Дольше всех ждёт {who}: {queue.age_label(oldest.created_at, now)}"
    return {
        "key": "late",
        "title": "Просрочено",
        "icon": "clock",
        "href": "#attention",
        "tab": "late",
        "value": count,
        "unit": _requests_word(count),
        "tone": "warn" if count else "quiet",
        "foot": foot,
        "legend": [],
        "bar": None,
    }


def _open_kpi(count: int, new_count: int) -> dict[str, Any]:
    """«Открытые заявки»: всего и полоса «новые / в работе»."""
    return {
        "key": "open",
        "title": "Открытые заявки",
        "icon": "inbox",
        "href": "#attention",
        "tab": "all",
        "value": count,
        "unit": _requests_word(count),
        "tone": None,
        "foot": None,
        "legend": [
            {"label": "Новые", "value": new_count, "tone": "new"},
            {"label": "В работе", "value": count - new_count, "tone": "progress"},
        ],
        # Полоса — <progress>: доля новых среди открытых (CSP не пускает style=).
        "bar": {"value": new_count, "max": count} if count else None,
    }


async def _today_kpi(href: str, local_now: datetime) -> dict[str, Any]:
    """«Сегодня в отеле»: заезды и выезды по местному дню отеля.

    Оба числа — по живущим сейчас (`checked_in`): выехавший уже не «выезжает
    сегодня»; гость, заселённый и выселенный в один день, в заезды не
    попадает — для гостиницы без дневных номеров это пренебрежимо, а лишнего
    запроса к модулю guests ради него не заводим. Броней у нас нет (с Opera не
    связаны), поэтому заезд — это уже заселённый сегодня гость.
    """
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    stays = await guests_api.list_active_stays()
    arrived = sum(1 for stay in stays if day_start <= stay.check_in_at < day_end)
    leaving = sum(1 for stay in stays if day_start <= stay.check_out_at < day_end)
    return {
        "key": "today",
        "title": "Сегодня в отеле",
        "icon": "key",
        "href": href,
        "tab": None,
        "value": arrived,
        "unit": plural(arrived, "заезд", "заезда", "заездов"),
        "tone": None if arrived else "quiet",
        "foot": f"Выездов сегодня: {leaving}",
        "legend": [],
        "bar": None,
    }


async def _turnovers_kpi(href: str, config: TenantConfig | None) -> dict[str, Any]:
    """«Выезды и уборка» (#606): номеров в пути, непустые шаги доски и «Готовы
    сегодня» — числа и названия шагов берёт у доски (`turnovers.board_counts`)."""
    steps, ready_today = await turnovers.board_counts(config)
    in_work = sum(count for _, _, count in steps)
    return {
        "key": "turnovers",
        "title": "Выезды и уборка",
        "icon": "broom",
        "href": href,
        "tab": None,
        "value": in_work,
        "unit": plural(in_work, "номер в работе", "номера в работе", "номеров в работе"),
        "tone": None if in_work else "quiet",
        "foot": f"Готовы сегодня: {ready_today}",
        "legend": [
            {"label": title, "value": count, "tone": _TURNOVER_TONES.get(key, "muted")}
            for key, title, count in steps
        ],
        "bar": None,
    }


def _attention(
    requests: list[requests_api.ServiceRequestRead],
    *,
    overdue_ids: set[Any],
    stalled_ids: set[Any],
    tracked: bool,
    names: dict[Any, str],
    now: datetime,
    tenant_slug: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """«Требует внимания»: вкладки со счётчиками и строки дольше всех ждущих.

    Одна лента, старшие сверху; у строки — вкладки, в которых она видна (в
    каждой — не больше `ATTENTION_ROWS` первых). Переключает вкладки
    `home.js` без запроса к серверу; без JS видна вкладка по умолчанию.
    """
    new = requests_api.RequestStatus.NEW
    members: dict[str, list[requests_api.ServiceRequestRead]] = {
        "late": [r for r in requests if r.id in overdue_ids],
        "new": [r for r in requests if r.status is new],
        "progress": [r for r in requests if r.status is not new],
        "all": list(requests),
    }
    tabs: list[dict[str, Any]] = [
        {
            "key": key,
            "label": label,
            "empty": empty,
            "count": len(members[key]),
            "more": max(0, len(members[key]) - ATTENTION_ROWS),
            "warn": key == "late",
        }
        for key, label, empty in _TABS
        if key != "late" or tracked
    ]
    shown: dict[Any, list[str]] = {}
    for tab in tabs:
        oldest = sorted(members[tab["key"]], key=lambda r: r.created_at)[:ATTENTION_ROWS]
        for request in oldest:
            shown.setdefault(request.id, []).append(tab["key"])
    rows = [
        _row(request, shown[request.id], overdue_ids, stalled_ids, names, now, tenant_slug)
        for request in sorted(requests, key=lambda r: r.created_at)
        if request.id in shown
    ]
    return tabs, rows


def _row(
    request: requests_api.ServiceRequestRead,
    tabs: list[str],
    overdue_ids: set[Any],
    stalled_ids: set[Any],
    names: dict[Any, str],
    now: datetime,
    tenant_slug: str,
) -> dict[str, Any]:
    """Строка: комната, суть, служба и номер, метка, сколько ждёт, кто взял.

    Возраст невзятой — от создания, взятой — от взятия: «в работе 5 мин»
    говорит о том, сколько её делают, а не сколько она живёт. Ссылка — в
    очередь поиском по номеру заявки (или комнате): там все её действия.
    """
    is_new = request.status is requests_api.RequestStatus.NEW
    late = request.id in overdue_ids or request.id in stalled_ids
    if is_new:
        tone, label = ("overdue", "Просрочена") if late else ("new", "Новая")
        since = request.created_at
    else:
        tone = "overdue" if late else "in_progress"
        label = queue.STALLED_LABEL.capitalize() if late else "В работе"
        since = request.claimed_at or request.created_at
    number = f"#{request.daily_number}" if request.daily_number is not None else None
    search = number or request.room_number or ""
    who = (request.claimed_by_display_name or "").strip()
    return {
        "id": str(request.id),
        "tabs": tabs,
        "room": request.room_number or "—",
        "summary": request.summary,
        "meta": " · ".join(part for part in (names.get(request.category_id), number) if part),
        "tone": tone,
        "label": label,
        "late": late,
        "urgent": request.is_urgent,
        "wait": queue.age_label(since, now),
        "wait_prefix": "Ждёт" if is_new else "В работе",
        "href": queue_filters.queue_path(
            tenant_slug,
            tab=queue_filters.QUEUE_TAB_OPEN,
            category_key=None,
            mine=False,
            search=search,
        ),
        "can_claim": is_new,
        "assignee": who or None,
        "assignee_initials": "".join(word[0] for word in who.split()[:2]).upper(),
    }

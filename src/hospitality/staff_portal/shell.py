"""Каркас страниц отеля: меню, профиль, нижняя панель телефона (v3 — issue #635).

Вид — макет основателя кабинета v3 (02.11.2026; до него — `staff-dashboard-v2`,
#462): разделы группами «Работа» и «Управление», текущий подсвечен, у очереди —
число открытых заявок; внизу профиль с темой и выходом. На телефоне — нижняя
панель на четыре раздела роли и «Ещё» с остальными. Каркас рисует
`layout.html`, когда в контексте есть `shell`; кладёт его туда только
`router._hotel_page` — на странице входа, приглашения и «нет доступа» меню
нет (сессии или членства там нет).

Видимость разделов — мини-матрица docs/RBAC.md, одна на меню и главную (P-12):
очередь — всем, заселение и «Передача смены» — ресепшену и менеджеру, «Выезды и
уборка» и «Этажи» — ещё и хаускипингу, «Товары и меню» — службе каталога room service и
менеджеру (`catalog.can_edit_catalog`, spec 0038 Р-9), «Документы для АВР» —
бухгалтерии, ресепшену и менеджеру (при `ENABLE_GUEST_DOCUMENTS`), «Наставник» —
всем уровням, если он включён у отеля (`TenantConfig.mentor_enabled`), сводка,
журнал и «Статистика» (при `ENABLE_STAFF_STATS`) — старшему и менеджеру,
справочник отеля, вид гостевого чата (spec 0066) и сотрудники — менеджеру;
демо-разделы — только при своих выключателях («Номера», #603, и «Забытые вещи»,
#604, — всем); «Тестовые данные»
(#740) — менеджеру и только на staging демо-отеля (`demo_data.nav_visible`).
Демо-ссылки «Наставник» и «Кодекс отеля» (#628) — всем, если задан адрес;
открываются в новой вкладке, и «Наставник»-ссылки нет, когда у отеля включён
настоящий раздел; демо-ссылка «3D-модель» (#792) — так же, но только менеджеру.
Меню лишь прячет ссылки: право проверяет сама страница (`_page_context`), и
ссылка, показанная по ошибке, дала бы «нет доступа», а не чужие данные.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import (
    act_requests,
    catalog,
    demo_data,
    floors,
    mentor,
    new_request,
    scope,
    stats,
    team,
    turnovers,
)


def can_check_in(staff: StaffContext) -> bool:
    """Заселение и касса: служба ресепшена любого уровня или менеджер (spec 0045 §2).

    То же условие, что `require_role(StaffRole.MANAGER, or_reception=True)` у
    самих страниц; здесь — чтобы меню и главная не вели в «нет доступа».
    """
    return staff.is_reception or staff.role_key is StaffRole.MANAGER


# Шаблон страницы → подсвеченный пункт меню. Форма новой заявки — часть
# очереди; страницы без пункта (их нет) просто ничего не подсвечивают.
_ACTIVE_BY_TEMPLATE: Final = {
    "home.html": "home",
    "queue.html": "requests",
    "new_request.html": "requests",
    "service_forbidden.html": "requests",
    "checkin.html": "checkin",
    "turnovers.html": "turnovers",
    "floors.html": "floors",
    "catalog.html": "catalog",
    "act_requests.html": "act_requests",
    "mentor_chat.html": "mentor",
    "mentor_codex.html": "mentor",
    "mentor_stats.html": "mentor",
    "demo_handover.html": "handover",
    "demo_room_board.html": "rooms",
    "demo_lost_found.html": "lost_found",
    "summary.html": "summary",
    "knowledge.html": "knowledge",
    "chat_look.html": "chat_look",
    "changes.html": "changes",
    "stats.html": "stats",
    "team.html": "team",
    "demo_data.html": "demo_data",
}


# Группа «Управление»; остальные разделы — «Работа».
_ADMIN_KEYS: Final = ("knowledge", "chat_look", "changes", "stats", "team", "demo_data")

# Пункты-ссылки на другие сайты (#628, #792): `path` у них — полный адрес `https://`.
_EXTERNAL_KEYS: Final = ("mentor_link", "codex_link", "model_3d_link")


def nav_items(staff: StaffContext, config: TenantConfig | None) -> list[dict[str, Any]]:
    """Разделы, открытые роли, в порядке меню. `path` — хвост после /staff/{slug}.

    `config` — конфиг отеля (None до онбординга): «Товары и меню» есть, только
    пока каталог room service открыт и сотрудник вправе его править.
    """
    settings = get_settings()
    is_reception = can_check_in(staff)
    is_manager = staff.role_key is StaffRole.MANAGER
    # Сводка и журнал старшего — по его службам (spec 0045 §3).
    is_senior = is_manager or staff.role_key is StaffRole.SENIOR
    # Касса — демо целиком или только ручной бланк (#469, без пометки «демо»).
    handover_on = settings.enable_demo_handover or settings.enable_handover_manual
    # Выезды и уборка — ресепшен, хаускипинг и менеджер (spec 0046 §4), если
    # раздел включён (§5, `ENABLE_TURNOVER_BOARD`).
    can_see_turnovers = turnovers.board_enabled() and turnovers.rights_for(staff).can_view
    # Этажи — те же ресепшен, хаускипинг и менеджер (spec 0053 §5), если раздел
    # включён (§7, `ENABLE_HOUSEKEEPING_FLOORS`).
    can_see_floors = floors.section_enabled() and floors.rights_for(staff).can_view
    candidates: list[tuple[bool, str, str, str, str, bool]] = [
        # (видно, ключ, путь, название, иконка, демо)
        (True, "home", "", "Обзор", "grid", False),
        (True, "requests", "/requests", "Очередь заявок", "inbox", False),
        (is_reception, "checkin", "/checkin", "Заселение", "key", False),
        (can_see_turnovers, "turnovers", "/turnovers", "Выезды и уборка", "broom", False),
        (settings.enable_demo_room_board, "rooms", "/rooms", "Номера", "door", True),
        (can_see_floors, "floors", "/floors", "Этажи", "layers", False),
        # Забытые вещи (демо #604) — все вошедшие: находку пишет горничная, ищет ресепшен.
        (
            settings.enable_demo_lost_found,
            "lost_found",
            "/lost-found",
            "Забытые вещи",
            "tag",
            True,
        ),
        # Касса — у стойки ресепшена, рядом с заселением и выездами (#606).
        (
            is_reception and handover_on,
            "handover",
            "/handover",
            "Передача смены",
            "handover",
            settings.enable_demo_handover,
        ),
        (
            bool(catalog.editable_catalogs(staff, config)),
            "catalog",
            "/catalog",
            "Товары и меню",
            "tray",
            False,
        ),
        (
            act_requests.nav_visible(staff),
            "act_requests",
            "/act-requests",
            "Документы для АВР",
            "doc",
            False,
        ),
        (mentor.section_on(config), "mentor", "/mentor", "Наставник", "chat", False),
        # Демо-ссылки к показу 05.11 (#628): путь — внешний адрес из настроек.
        (
            bool(settings.demo_mentor_url) and not mentor.section_on(config),
            "mentor_link",
            settings.demo_mentor_url,
            "Наставник",
            "chat",
            True,
        ),
        (
            bool(settings.demo_codex_url),
            "codex_link",
            settings.demo_codex_url,
            "Кодекс отеля",
            "book",
            True,
        ),
        # 3D-витрина для групповых продаж (#792) — только менеджеру.
        (
            is_manager and bool(settings.demo_3d_url),
            "model_3d_link",
            settings.demo_3d_url,
            "3D-модель",
            "cube",
            True,
        ),
        (is_senior, "summary", "/summary", "Сводка дня", "chart", False),
        (is_manager, "knowledge", "/knowledge", "Справочник отеля", "book", False),
        (is_manager, "chat_look", "/chat-look", "Вид гостевого чата", "palette", False),
        (is_senior, "changes", "/changes", "Журнал изменений", "history", False),
        # Итоги по сотруднику (spec 0065 §2.3–§2.4) — при `ENABLE_STAFF_STATS`.
        (is_senior and stats.section_enabled(), "stats", "/stats", "Статистика", "trend", False),
        (is_manager, "team", "/team", "Сотрудники", "people", False),
        # Тестовые данные (#740) — только staging демо-отеля и менеджер.
        (
            demo_data.nav_visible(staff),
            "demo_data",
            "/demo-data",
            "Тестовые данные",
            "upload",
            False,
        ),
    ]
    return [
        {
            "key": key,
            "href": path if key in _EXTERNAL_KEYS else f"/staff/{staff.tenant_slug}{path}",
            "title": title,
            "icon": icon,
            "demo": demo,
            "group": "admin" if key in _ADMIN_KEYS else "work",
            # Внешний сайт: новая вкладка, значок ↗, подсветки текущего нет.
            "external": key in _EXTERNAL_KEYS,
        }
        for visible, key, path, title, icon, demo in candidates
        if visible
    ]


# Нижняя панель телефона (#635): до четырёх разделов + «Ещё» — первые видимые
# роли в этом порядке, с короткими подписями (пятая часть ширины 390 px).
# Остальные разделы, тема и выход — в листе «Ещё».
_BOTTOM_LABELS: Final = {
    "home": "Обзор",
    "requests": "Заявки",
    "checkin": "Заселение",
    "turnovers": "Уборка",
    "floors": "Этажи",
    "rooms": "Номера",
}
_BOTTOM_SLOTS: Final = 4
# Счётчик у «Очереди заявок»: больше — «99+»; столько же строк и читается.
OPEN_COUNT_CAP: Final = 99


async def open_requests_count(staff: StaffContext) -> int:
    """Открытые заявки (`new` + `in_progress`) служб сотрудника — число у пункта
    «Очередь заявок». Тот же срез, что у ленты очереди (spec 0045 §5); читается
    под RLS тенанта запроса. Больше `OPEN_COUNT_CAP` не считает: на значке «99+»."""
    allowed = await scope.load_allowed_category_ids(staff)
    requests = await requests_api.list_open_requests(limit=OPEN_COUNT_CAP + 1, category_ids=allowed)
    return len(requests)


def _count_label(count: int) -> str | None:
    if count <= 0:
        return None
    return f"{OPEN_COUNT_CAP}+" if count > OPEN_COUNT_CAP else str(count)


def shell_context(
    staff: StaffContext,
    template: str,
    config: TenantConfig | None,
    *,
    open_requests: int = 0,
    several_hotels: bool = False,
) -> dict[str, Any]:
    """Контекст каркаса для `layout.html`: меню, текущий пункт, карточка вошедшего,
    нижняя панель телефона. `several_hotels` — ссылка «Сменить отель» на выбор
    (`/staff/`, как на главной до #635); при одном членстве её нет."""
    items = nav_items(staff, config)
    active = _ACTIVE_BY_TEMPLATE.get(template)
    bottom_keys = [key for key in _BOTTOM_LABELS if any(item["key"] == key for item in items)]
    bottom_keys = bottom_keys[:_BOTTOM_SLOTS]
    by_key = {item["key"]: item for item in items}
    rest = [item for item in items if item["key"] not in bottom_keys]
    name = staff.display_name.strip()
    return {
        "tenant_name": staff.tenant_name,
        "tenant_initial": (staff.tenant_name.strip()[:1] or "·").upper(),
        "several_hotels": several_hotels,
        "work": [item for item in items if item["group"] == "work"],
        "admin": [item for item in items if item["group"] == "admin"],
        "active": active,
        "open_count": _count_label(open_requests),
        "bottom": [{**by_key[key], "short": _BOTTOM_LABELS[key]} for key in bottom_keys],
        "more_work": [item for item in rest if item["group"] == "work"],
        "more_admin": [item for item in rest if item["group"] == "admin"],
        # «Ещё» подсвечен, когда открыт раздел из него.
        "more_active": active is not None and active not in bottom_keys,
        # Круглая «+» — новая заявка; на самой её странице кнопка не нужна.
        "new_request_href": (
            None if template == "new_request.html" else f"/staff/{staff.tenant_slug}/requests/new"
        ),
        # Окно «Новая заявка» (#636) — там же, где «+»; поля оно берёт при открытии.
        "new_request_dialog": (
            None if template == "new_request.html" else new_request.form_paths(staff.tenant_slug)
        ),
        "user_name": name,
        "user_initial": (name[:1] or "·").upper(),
        "role_label": team.role_label(staff.role_key),
    }

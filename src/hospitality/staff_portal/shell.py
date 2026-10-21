"""Каркас страниц отеля: боковое меню и карточка «кто вошёл» (issue #462).

Вид — макет основателя `staff-dashboard-v2` (19.10.2026): разделы группами
«Работа» и «Управление», текущий подсвечен, внизу имя, роль и выход. Каркас
рисует `layout.html`, когда в контексте есть `shell`; кладёт его туда только
`router._hotel_page` — на странице входа, приглашения и «нет доступа» меню
нет (сессии или членства там нет).

Видимость разделов — мини-матрица docs/RBAC.md, одна на меню и главную (P-12):
очередь — всем, заселение и «Передача смены» — ресепшену и менеджеру, сводка,
справочник отеля, журнал и сотрудники — менеджеру; демо-разделы — только при своих
выключателях. Меню лишь прячет ссылки: право проверяет сама страница
(`_page_context`), и ссылка, показанная по ошибке, дала бы «нет доступа», а не
чужие данные.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import team

RECEPTION_ROLES: Final = (StaffRole.RECEPTIONIST, StaffRole.MANAGER)

# Шаблон страницы → подсвеченный пункт меню. Форма новой заявки — часть
# очереди; страницы без пункта (их нет) просто ничего не подсвечивают.
_ACTIVE_BY_TEMPLATE: Final = {
    "home.html": "home",
    "queue.html": "requests",
    "new_request.html": "requests",
    "checkin.html": "checkin",
    "demo_orders.html": "orders",
    "demo_handover.html": "handover",
    "summary.html": "summary",
    "knowledge.html": "knowledge",
    "changes.html": "changes",
    "team.html": "team",
}


def nav_items(staff: StaffContext) -> list[dict[str, Any]]:
    """Разделы, открытые роли, в порядке меню. `path` — хвост после /staff/{slug}."""
    settings = get_settings()
    is_reception = staff.role_key in RECEPTION_ROLES
    is_manager = staff.role_key is StaffRole.MANAGER
    candidates: list[tuple[bool, str, str, str, str, bool]] = [
        # (видно, ключ, путь, название, иконка, демо)
        (True, "home", "", "Обзор", "grid", False),
        (True, "requests", "/requests", "Очередь заявок", "inbox", False),
        (is_reception, "checkin", "/checkin", "Заселение", "key", False),
        (settings.enable_demo_orders, "orders", "/orders", "Заказы в номер", "tray", True),
        (
            is_reception and settings.enable_demo_handover,
            "handover",
            "/handover",
            "Передача смены",
            "handover",
            True,
        ),
        (is_manager, "summary", "/summary", "Сводка дня", "chart", False),
        (is_manager, "knowledge", "/knowledge", "Справочник отеля", "book", False),
        (is_manager, "changes", "/changes", "Журнал изменений", "history", False),
        (is_manager, "team", "/team", "Сотрудники", "people", False),
    ]
    return [
        {
            "key": key,
            "href": f"/staff/{staff.tenant_slug}{path}",
            "title": title,
            "icon": icon,
            "demo": demo,
            "group": "admin" if key in ("knowledge", "changes", "team") else "work",
        }
        for visible, key, path, title, icon, demo in candidates
        if visible
    ]


def shell_context(staff: StaffContext, template: str) -> dict[str, Any]:
    """Контекст каркаса для `layout.html`: меню, текущий пункт, карточка вошедшего."""
    items = nav_items(staff)
    name = staff.display_name.strip()
    return {
        "tenant_name": staff.tenant_name,
        "tenant_initial": (staff.tenant_name.strip()[:1] or "·").upper(),
        "work": [item for item in items if item["group"] == "work"],
        "admin": [item for item in items if item["group"] == "admin"],
        "active": _ACTIVE_BY_TEMPLATE.get(template),
        "user_name": name,
        "user_initial": (name[:1] or "·").upper(),
        "role_label": team.role_label(staff.role_key),
    }

"""Каркас страниц отеля: боковое меню и карточка «кто вошёл» (issue #462).

Вид — макет основателя `staff-dashboard-v2` (19.10.2026): разделы группами
«Работа» и «Управление», текущий подсвечен, внизу имя, роль и выход. Каркас
рисует `layout.html`, когда в контексте есть `shell`; кладёт его туда только
`router._hotel_page` — на странице входа, приглашения и «нет доступа» меню
нет (сессии или членства там нет).

Видимость разделов — мини-матрица docs/RBAC.md, одна на меню и главную (P-12):
очередь — всем, заселение и «Передача смены» — ресепшену и менеджеру, «Выезды и
уборка» — ещё и хаускипингу, сводка
и журнал — старшему и менеджеру, справочник отеля и сотрудники — менеджеру;
демо-разделы — только при своих выключателях. Меню лишь прячет ссылки: право проверяет сама страница
(`_page_context`), и ссылка, показанная по ошибке, дала бы «нет доступа», а не
чужие данные.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import team, turnovers


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
    is_reception = can_check_in(staff)
    is_manager = staff.role_key is StaffRole.MANAGER
    # Сводка и журнал старшего — по его службам (spec 0045 §3).
    is_senior = is_manager or staff.role_key is StaffRole.SENIOR
    # Касса — демо целиком или только ручной бланк (#469, без пометки «демо»).
    handover_on = settings.enable_demo_handover or settings.enable_handover_manual
    # Выезды и уборка — ресепшен, хаускипинг и менеджер (spec 0046 §4), если
    # раздел включён (§5, `ENABLE_TURNOVER_BOARD`).
    can_see_turnovers = turnovers.board_enabled() and turnovers.rights_for(staff).can_view
    candidates: list[tuple[bool, str, str, str, str, bool]] = [
        # (видно, ключ, путь, название, иконка, демо)
        (True, "home", "", "Обзор", "grid", False),
        (True, "requests", "/requests", "Очередь заявок", "inbox", False),
        (is_reception, "checkin", "/checkin", "Заселение", "key", False),
        (can_see_turnovers, "turnovers", "/turnovers", "Выезды и уборка", "broom", False),
        (settings.enable_demo_orders, "orders", "/orders", "Заказы в номер", "tray", True),
        (
            is_reception and handover_on,
            "handover",
            "/handover",
            "Передача смены",
            "handover",
            settings.enable_demo_handover,
        ),
        (is_senior, "summary", "/summary", "Сводка дня", "chart", False),
        (is_manager, "knowledge", "/knowledge", "Справочник отеля", "book", False),
        (is_senior, "changes", "/changes", "Журнал изменений", "history", False),
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

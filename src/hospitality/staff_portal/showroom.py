"""Раздел «3D-витрина» кабинета (spec 0069 §2.2, §6 PR а; issue #803).

Просмотрщик и панель подбора — тот же `showroom.js`, что у страницы посетителя, но
сцену и подбор он берёт из кабинета (`showroom_api.py`) под сессией: в кабинетном
режиме страницы посетителя нет вовсе. Менеджер ещё загружает сцену файлом JSON и
открывает страницу посетителя без кода (cookie доступа под сессией кабинета).

Права (docs/RBAC.md) — одна функция `can_view` на меню, страницу и действия:
менеджер и сотрудники служб витрины (`ShowroomConfig.sales_category_key`,
`booking_category_key`) любого уровня. Загрузка сцены и cookie посетителя — только
менеджер (`require_role`). Витрина выключена у отеля — пункта меню нет, страница и
действия отвечают 404, как неизвестный адрес. Пока раздел включён, демо-ссылки
«3D-модель ↗» (#792) в меню нет: её место занимает настоящий раздел.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import HTTPException

from hospitality.channels.showroom import assets
from hospitality.channels.showroom.page import config_json, viewer_config
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.queue import tenant_config

TITLE: Final = "3D-витрина"
# Тексты кабинета — spec 0069 §5 «Кабинет»; пустая сцена — надписи окна PR (а).
UPLOAD_LABEL: Final = "Загрузить сцену (JSON)"
OPEN_VISITOR_LABEL: Final = "Открыть страницу посетителя"
UPDATED_TEXT: Final = "Сцена обновлена: версия {version}, {date}"
REJECTED_TEXT: Final = "Сцена не принята: {reason}"
NO_SCENE_MANAGER_TEXT: Final = "Сцена ещё не загружена — загрузите файл JSON со зданием отеля."
NO_SCENE_STAFF_TEXT: Final = "Сцена ещё не загружена — её загружает менеджер."


def section_on(config: TenantConfig | None) -> bool:
    """Витрина включена у отеля (`TenantConfig.showroom.enabled`)."""
    return config is not None and config.showroom is not None and config.showroom.enabled


def can_view(staff: StaffContext, settings: ShowroomConfig) -> bool:
    """Менеджер или сотрудник службы продаж или бронирования отеля."""
    if staff.role_key is StaffRole.MANAGER:
        return True
    return bool(staff.service_keys & settings.category_keys)


def nav_visible(staff: StaffContext, config: TenantConfig | None) -> bool:
    """Пункт меню «3D-витрина»: раздел включён и сотруднику он открыт."""
    return (
        section_on(config)
        and config is not None
        and config.showroom is not None
        and (can_view(staff, config.showroom))
    )


async def require_section(staff: StaffContext) -> ShowroomConfig | None:
    """Настройки витрины, если раздел включён; выключен — 404. Чужой службе — None."""
    config = await tenant_config(staff)
    if not section_on(config) or config is None or config.showroom is None:
        raise HTTPException(status_code=404)
    return config.showroom if can_view(staff, config.showroom) else None


def visitor_page_open(settings: ShowroomConfig) -> bool:
    """Страница посетителя видна снаружи: есть код доступа или публичный режим."""
    return settings.public_enabled or settings.access_code_hash is not None


async def page_context(staff: StaffContext, settings: ShowroomConfig) -> dict[str, Any]:
    """Контекст `showroom.html`: сцена есть ли, кнопки менеджера, настройки просмотрщика."""
    config = await tenant_config(staff)
    current = await showroom_api.get_scene()
    base = f"/staff/{staff.tenant_slug}/showroom"
    is_manager = staff.role_key is StaffRole.MANAGER
    updated = None
    if current is not None and config is not None:
        updated = UPDATED_TEXT.format(
            version=current.scene_version,
            date=format_local(current.replaced_at, config.tzinfo),
        )
    viewer = viewer_config(language="ru", scene_url=f"{base}/scene", rooms_url=f"{base}/rooms")
    return {
        "title": TITLE,
        "tenant_name": staff.tenant_name,
        "is_manager": is_manager,
        "has_scene": current is not None,
        "updated_text": updated,
        "no_scene_text": NO_SCENE_MANAGER_TEXT if is_manager else NO_SCENE_STAFF_TEXT,
        "upload_label": UPLOAD_LABEL,
        "upload_endpoint": f"{base}/scene",
        "open_visitor": is_manager and visitor_page_open(settings),
        "open_visitor_label": OPEN_VISITOR_LABEL,
        "access_cookie_endpoint": f"{base}/access-cookie",
        "visitor_url": f"/showroom/{staff.tenant_slug}",
        "viewer_config_json": config_json(viewer),
        "showroom_css_url": assets.url("showroom.css"),
        "showroom_js_url": assets.url("showroom.js"),
        "updated_template": UPDATED_TEXT,
        "rejected_template": REJECTED_TEXT,
    }

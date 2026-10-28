"""Панель «Товары и меню» (spec 0038 §6 PR 4, Р-9, Р-10; issue #418): права и вид.

Каталоги room service — ресторан и магазин — ведут сами службы: менеджер и
сотрудник любого уровня со службой каталога (`category_key` из конфига отеля).
Право — одна функция `can_edit_catalog` на пункт меню, страницу и JSON-действия
(P-12; ролей на службу нет — spec 0045 §2). Данные и правила каталога — модуль
`room_service`; здесь только то, что знает кабинет: кто правит какой каталог и
как страница их показывает. Фото товаров — PR 7 серии, заказы — PR 5–6.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from fastapi import HTTPException

from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import LocalizedText, TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import ERR_AUTH_FORBIDDEN, StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import queue

logger = get_logger(module=__name__)

Catalog = room_service_api.RoomServiceCatalog

# Порядок вкладок — порядок перечисления модуля, а не ключей конфига: JSONB
# переставляет ключи объекта (ревью PR #508, П-13).
CATALOG_TITLES: Final[dict[Catalog, str]] = {
    Catalog.RESTAURANT: "Ресторан",
    Catalog.SHOP: "Магазин",
}
_ACCEPTING_HINTS: Final[dict[Catalog, str]] = {
    Catalog.RESTAURANT: (
        "Выключите, когда кухня закрыта: гости увидят меню, но не смогут оформить заказ."
    ),
    Catalog.SHOP: (
        "Выключите, когда некому нести: гости увидят товары, но не смогут оформить заказ."
    ),
}
# Подписи языков во вкладках формы; `en` обязателен (spec 0038 Р-6).
LANGUAGE_LABELS: Final[dict[str, str]] = {"en": "EN", "ru": "RU", "kk": "KK"}
# Язык названий в самой панели: персонал читает по-русски, перевода нет — EN.
_PANEL_LANGUAGE: Final = "ru"
_NBSP: Final = " "
_MINOR_PER_TENGE: Final = 100


def can_edit_catalog(staff: StaffContext, catalog: Catalog, config: TenantConfig | None) -> bool:
    """Право править каталог (Р-9): каталог открыт конфигом отеля, и сотрудник —
    менеджер или в службе каталога. Закрытый каталог не правит никто."""
    if config is None:
        return False
    settings = config.room_service_catalog(catalog.value)
    if settings is None:
        return False
    return staff.role_key is StaffRole.MANAGER or settings.category_key in staff.service_keys


def editable_catalogs(staff: StaffContext, config: TenantConfig | None) -> list[Catalog]:
    """Каталоги, которые сотрудник правит, — в порядке вкладок."""
    return [catalog for catalog in Catalog if can_edit_catalog(staff, catalog, config)]


def room_service_on(config: TenantConfig | None) -> bool:
    """Открыт ли у отеля хоть один каталог: иначе раздела нет совсем (404, Р-10)."""
    return config is not None and any(
        config.room_service_catalog(catalog.value) is not None for catalog in Catalog
    )


def require_edit(staff: StaffContext, catalog: Catalog, config: TenantConfig | None) -> None:
    """JSON-действие над чужим каталогом — ERR-AUTH-003 (403), как страница."""
    if not can_edit_catalog(staff, catalog, config):
        logger.warning(
            "staff.catalog_denied",
            catalog=catalog.value,
            user_id=str(staff.user_id),
            role=staff.role_key.value,
        )
        raise AppError(
            code=ERR_AUTH_FORBIDDEN,
            message="This catalog is edited by another service",
            status_code=403,
        )


def catalog_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/catalog"


def parse_catalog(value: str | None, allowed: list[Catalog]) -> Catalog | None:
    """Вкладка из `?catalog=`: без параметра — первая своя; чужая или
    неизвестная — None (страница ответит «нет доступа», как чужой фильтр очереди)."""
    if value is None:
        return allowed[0] if allowed else None
    try:
        catalog = Catalog(value)
    except ValueError:
        return None
    return catalog if catalog in allowed else None


async def page_target(
    staff: StaffContext, requested: str | None
) -> tuple[TenantConfig, Catalog] | None:
    """Конфиг и вкладка страницы и её фрагмента (одно правило на оба адреса).

    Room service у отеля выключен — 404, как неизвестный адрес: раздела нет
    совсем (Р-10). Своих каталогов нет или вкладка чужая — None: «нет доступа».
    """
    config = await queue.tenant_config(staff)
    if config is None or not room_service_on(config):
        raise HTTPException(status_code=404)
    target = parse_catalog(requested, editable_catalogs(staff, config))
    return None if target is None else (config, target)


async def build_catalog_context(
    staff: StaffContext, config: TenantConfig, catalog: Catalog
) -> dict[str, Any]:
    """Вкладки, переключатель «принимаем», числа и разделы с карточками.

    Тот же контекст рисует и страница, и фрагмент после каждого действия.
    """
    allowed = editable_catalogs(staff, config)
    views = {
        each: await room_service_api.get_catalog(each, include_hidden=True) for each in allowed
    }
    settings = await room_service_api.get_settings(catalog)
    categories = {category.key: category.name for category in await requests_api.list_categories()}
    items = [item for section in views[catalog].sections for item in section.items]
    hidden = sum(1 for item in items if item.hidden_at is not None)
    sold_out = sum(1 for item in items if item.hidden_at is None and not item.is_available)
    room_service = config.room_service
    languages = room_service.languages if room_service is not None else ("en",)
    base = catalog_path(staff.tenant_slug)
    return {
        "tenant_slug": staff.tenant_slug,
        "tenant_name": staff.tenant_name,
        "tabs": [
            {
                "key": each.value,
                "title": CATALOG_TITLES[each],
                "count": sum(
                    1
                    for section in views[each].sections
                    for item in section.items
                    if item.hidden_at is None
                ),
                "href": f"{base}?catalog={each.value}",
                "active": each is catalog,
            }
            for each in allowed
        ],
        "catalog": catalog.value,
        "accepting": settings.accepting_orders,
        "accepting_hint": _ACCEPTING_HINTS[catalog],
        "service_line": _service_line(
            categories.get(settings.category_key, settings.category_key),
            settings.service_charge_percent,
        ),
        "stats": {
            "available": len(items) - hidden - sold_out,
            "sold_out": sold_out,
            "hidden": hidden,
        },
        "sections": [
            _section(section, index, len(views[catalog].sections))
            for index, section in enumerate(views[catalog].sections)
        ],
        "languages": [{"code": code, "label": LANGUAGE_LABELS[code]} for code in languages],
        "fragment_path": f"{base}/fragment?catalog={catalog.value}",
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/catalog",
    }


def _section(section: room_service_api.SectionWithItems, index: int, total: int) -> dict[str, Any]:
    return {
        "id": str(section.id),
        "title": _panel_text(section.names),
        "names": section.names.as_json(),
        "count": sum(1 for item in section.items if item.hidden_at is None),
        "first": index == 0,
        "last": index == total - 1,
        "cards": [
            _item(item, position, len(section.items)) for position, item in enumerate(section.items)
        ],
    }


def _item(item: room_service_api.ItemRead, index: int, total: int) -> dict[str, Any]:
    hidden = item.hidden_at is not None
    return {
        "id": str(item.id),
        "title": _panel_text(item.names),
        "description": _panel_text(item.descriptions) if item.descriptions else "",
        "price_label": money_label(item.price_minor),
        "available": item.is_available,
        "hidden": hidden,
        "first": index == 0,
        "last": index == total - 1,
        # Поиск на странице — по названию на любом языке отеля.
        "search": " ".join(item.names.as_json().values()).lower(),
        # Всё, что нужно форме «Изменить товар», — в data-атрибут карточки:
        # все переводы, даже тех языков, которых нет во вкладках отеля, — форма
        # отправит их обратно, а не сотрёт молча.
        "form": {
            "id": str(item.id),
            "section_id": str(item.section_id),
            "names": item.names.as_json(),
            "descriptions": item.descriptions.as_json() if item.descriptions else {},
            "price": item.price_minor // _MINOR_PER_TENGE,
            "is_available": item.is_available,
            "hidden": hidden,
        },
    }


def _panel_text(text: LocalizedText) -> str:
    return text.text_for(_PANEL_LANGUAGE)


def money_label(minor: int) -> str:
    """«2 900 ₸» — целые тенге с неразрывными пробелами (spec 0038 §5, ru)."""
    return f"{minor // _MINOR_PER_TENGE:,}".replace(",", _NBSP) + f"{_NBSP}₸"


def _service_line(service_name: str, percent: Decimal) -> str:
    """«Ведёт служба «Ресторан» · сервисный сбор 10% — настройка отеля»."""
    if percent == 0:
        return f"Ведёт служба «{service_name}» · без сервисного сбора"
    number = format(percent.normalize(), "f").replace(".", ",")
    return f"Ведёт служба «{service_name}» · сервисный сбор {number}% — настройка отеля"

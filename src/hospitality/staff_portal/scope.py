"""Граница службы в кабинете (spec 0045 §3, §5): чьи заявки сотрудник видит и ведёт.

Сотрудник и старший видят и ведут заявки своих служб, ресепшен и менеджер —
всех (`StaffContext.sees_all_services`). Право держит сервер, а не меню:
очередь, «закрытые за сегодня», главная и сигнал читают заявки с
`category_ids` (срез в SQL), действия передают домену `allowed_category_ids`,
и он проверяет службу под блокировкой строки (`requests_api`).

Здесь только перевод служб членства (ключи категорий) в id категорий отеля:
ключи лежат на членстве (платформенная таблица), id — у модуля requests (R-5).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffAudience, StaffContext

# Сотрудник без служб (после миграции 0033, spec 0045 §7) заявок не видит —
# очередь и главная говорят, что делать (§3).
NO_SERVICES_TEXT: Final = (
    "У вас не отмечена служба, поэтому заявок здесь нет. Попросите менеджера "
    "отметить её на странице «Сотрудники». Принять заявку гостя можно и сейчас — "
    "кнопка «Новая заявка»."
)


def allowed_category_ids(
    staff: StaffAudience, categories: Iterable[requests_api.RequestCategoryRead]
) -> frozenset[uuid.UUID] | None:
    """Id категорий служб сотрудника; None — все службы (ресепшен, менеджер).

    Ключ службы, у которого в отеле нет категории (категорию удалили), ничего не
    открывает (spec 0045 §2): в ответ попадают только существующие категории.
    """
    if staff.sees_all_services:
        return None
    return frozenset(category.id for category in categories if category.key in staff.service_keys)


async def load_allowed_category_ids(staff: StaffContext) -> frozenset[uuid.UUID] | None:
    """То же для действия над заявкой: категории читаются, только если они нужны."""
    if staff.sees_all_services:
        return None
    return allowed_category_ids(staff, await requests_api.list_categories())


def has_no_services(staff: StaffContext) -> bool:
    """Сотрудник без единой службы: очередь и главная показывают `NO_SERVICES_TEXT`."""
    return not staff.sees_all_services and not staff.service_keys


def is_foreign_service(staff: StaffContext, category_key: str) -> bool:
    """Фильтр `?category=` указывает на службу не из списка сотрудника (403)."""
    return not staff.sees_all_services and category_key not in staff.service_keys


def own_services_label(
    staff: StaffAudience, categories: Iterable[requests_api.RequestCategoryRead]
) -> str | None:
    """«Уборка, Прачечная» — службы, которыми срезаны сводка и журнал старшего;
    None — срезано не было (ресепшен и менеджер видят весь отель)."""
    if staff.sees_all_services:
        return None
    names = [category.name for category in categories if category.key in staff.service_keys]
    return ", ".join(names) or "не отмечены"

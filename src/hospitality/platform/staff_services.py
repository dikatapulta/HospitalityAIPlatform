"""Службы сотрудника: что это и как их проверять при записи (spec 0045 §2, §6).

Служба — категория заявки отеля (`request_categories.key`), на членстве и в
приглашении лежит списком ключей (`service_keys`). Здесь — единственные правила
про этот список (P-12): какой ключ — ресепшен и уборка, кому службы положены и как
список приводится к виду для записи. Существующие у отеля ключи сюда передаёт
вызывающий (кабинет читает их из модуля requests): ядро модули не импортирует
(R-5).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Final

from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError

# Ресепшен — служба, а не роль (spec 0045 §2): её сотрудник любого уровня видит
# все заявки и заселяет гостей. Ключ не настраивается — ключи категорий выбирает
# онбординг, у профиля пилота он `reception`.
RECEPTION_SERVICE_KEY: Final = "reception"

# Служба уборки — её сотрудники ведут номер после выезда до «готов к заселению»
# (spec 0046 §3). Ключ не настраивается по тому же доводу, что у ресепшена:
# у профиля пилота он `housekeeping`.
HOUSEKEEPING_SERVICE_KEY: Final = "housekeeping"

# Код каталога (docs/runbooks/errors.md): набор служб не годится для уровня.
ERR_AUTH_INVALID_SERVICES: Final = "ERR-AUTH-015"


def has_reception(service_keys: Iterable[str]) -> bool:
    """Есть ли среди служб ресепшен — право заселять и видеть все заявки."""
    return RECEPTION_SERVICE_KEY in service_keys


def has_housekeeping(service_keys: Iterable[str]) -> bool:
    """Есть ли среди служб уборка — шаги пути номера после выезда (spec 0046 §4)."""
    return HOUSEKEEPING_SERVICE_KEY in service_keys


def sees_all_services(role: StaffRole, service_keys: Iterable[str]) -> bool:
    """Ресепшен и менеджер видят и ведут заявки всех служб (spec 0045 §3).

    Они же — адресаты третьей ступени лестницы невзятой заявки (§4). Остальные
    видят и ведут только заявки своих служб.
    """
    return role is StaffRole.MANAGER or has_reception(service_keys)


def _invalid_services(message: str) -> AppError:
    return AppError(code=ERR_AUTH_INVALID_SERVICES, message=message, status_code=422)


def normalize_service_keys(
    role: StaffRole,
    service_keys: Iterable[str],
    *,
    known_service_keys: Collection[str],
) -> list[str]:
    """Список служб к записи: без повторов, по алфавиту, только службы отеля.

    Менеджеру службы не положены — он видит всё, и список пишется пустым,
    что бы ни пришло. Сотруднику и старшему нужна хотя бы одна служба, иначе
    он не видел бы ни одной заявки. Ключ не из категорий отеля — отказ, а не
    молчаливый пропуск: принятая опечатка выглядела бы рабочей настройкой.
    """
    if role is StaffRole.MANAGER:
        return []
    keys = sorted(set(service_keys))
    if not keys:
        raise _invalid_services("Staff and senior need at least one service")
    if any(key not in known_service_keys for key in keys):
        raise _invalid_services("Unknown service for this hotel")
    return keys

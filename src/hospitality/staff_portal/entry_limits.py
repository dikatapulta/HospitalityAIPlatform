"""Ограда ручного ввода кабинета от заливки (#971, находки О2-1 и О2-2 аудита 10.11).

«Новая заявка» открыта любой учётке в любую службу (docs/RBAC.md §2), «Отметить
выезд» — ресепшену и менеджеру. Без предела одна учётка за полсекунды заливала
полсотни заявок или сотни выдуманных номеров, и настоящая заявка или выезд выпадали
из сигнала и доски. Предел — канон `ratelimit.consume_rate_limit` на учётку, окно
10 минут; числа — с запасом к живому темпу (обоснование — в PR #1116 и
`docs/founder-review/2026-11-12-pr1116-manual-entry-limits.md`). Redis недоступен —
ввод идёт (fail-open канона, spec 0023 §2).

Номер выезда сверяется со списком номеров отеля (`TenantConfig.hotel_rooms`,
spec 0061 §2), когда список загружен: тогда открытых путей не больше, чем номеров, —
одна строка на номер держит домен (`uq_room_turnovers_tenant_room_open`).
"""

from __future__ import annotations

from typing import Final

from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_MANUAL_RATE_LIMITED: Final = "ERR-REQUESTS-011"
ERR_HOUSEKEEPING_UNKNOWN_ROOM: Final = "ERR-HOUSEKEEPING-008"
ERR_HOUSEKEEPING_REPORT_RATE_LIMITED: Final = "ERR-HOUSEKEEPING-009"

# Заявок вручную у одной учётки за окно: весь отель — 50–120 заявок в сутки, человек
# заполняет форму за 20–30 с; 20 за 10 минут — заявка каждые 30 с десять минут подряд.
MANUAL_REQUESTS_PER_WINDOW: Final = 20
# Отметок выезда у одной учётки за окно: групповой выезд — 40+ номеров разом, у стойки
# одна учётка на смену, у гостя в кабинете — второе нажатие «выселить»
# (ERR-HOUSEKEEPING-004). Предел держит заливку, а не темп (ответ диспетчера 12.11).
TURNOVER_REPORTS_PER_WINDOW: Final = 120
WINDOW_SECONDS: Final = 600


async def enforce_manual_request_limit(staff: StaffContext) -> None:
    """21-я заявка вручную за окно — ERR-REQUESTS-011 (429), заявка не создаётся."""
    await _enforce(
        staff,
        scope="staff_manual_request",
        limit=MANUAL_REQUESTS_PER_WINDOW,
        code=ERR_REQUESTS_MANUAL_RATE_LIMITED,
        message="Too many manual requests from this account — wait a few minutes",
    )


async def enforce_turnover_report_limit(staff: StaffContext) -> None:
    """Отметка выезда сверх предела окна — ERR-HOUSEKEEPING-009 (429)."""
    await _enforce(
        staff,
        scope="staff_turnover_report",
        limit=TURNOVER_REPORTS_PER_WINDOW,
        code=ERR_HOUSEKEEPING_REPORT_RATE_LIMITED,
        message="Too many checkout reports from this account — wait a few minutes",
    )


def ensure_known_room(config: TenantConfig | None, room_number: str) -> None:
    """Номера нет в загруженном списке отеля — ERR-HOUSEKEEPING-008 (422).

    Список пуст (не загружен) — проверки нет: доска знает только номера с гостями и
    уборкой (spec 0061 §2). Сравнение — строка в строку, как у доски «Номера».
    """
    if config is None or not config.hotel_rooms:
        return
    if any(room.number == room_number for room in config.hotel_rooms):
        return
    raise AppError(
        code=ERR_HOUSEKEEPING_UNKNOWN_ROOM,
        message="This room number is not in the hotel's room list",
        status_code=422,
    )


async def _enforce(staff: StaffContext, *, scope: str, limit: int, code: str, message: str) -> None:
    # Ключ начинается с tenant_id (P-4: у Redis нет RLS).
    decision = await consume_rate_limit(
        scope, f"{staff.tenant_id}:{staff.user_id}", limit=limit, window_seconds=WINDOW_SECONDS
    )
    if decision.available and not decision.allowed:
        logger.warning(
            "staff.entry_rate_limited",
            scope=scope,
            user_id=str(staff.user_id),
            count=decision.count,
            limit=limit,
        )
        raise AppError(code=code, message=message, status_code=429)

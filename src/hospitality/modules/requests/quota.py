"""Счёт квоты бесплатного на сегодня (spec 0056 Р-2, Р-3, §4).

Отдельный файл, а не функция в `service.py`: тот за границей R-3 — довод
`payment.py`. Вход снаружи — реэкспорт через `api.py` (контракт import-linter).
Правило квоты (норма, службы, цена) — данные тенанта
(`platform.service_quotas`); здесь только «сколько уже заказано» и остаток.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from hospitality.modules.requests.models import RequestStatus, ServiceRequest
from hospitality.modules.requests.schemas import QuotaCheck
from hospitality.modules.requests.service import _hotel_service_day
from hospitality.platform.service_quotas import ServiceQuota
from hospitality.shared.db import session_scope


async def check_quota(
    rule: ServiceQuota, *, stay_id: uuid.UUID, guests_count: int, units: int
) -> QuotaCheck:
    """Предел, уже заказанное и бесплатный остаток квоты `rule` для проживания.

    Предел — норма × `guests_count` (Р-2). Заказано (Р-3) — сумма
    `quota_units` заявок этого Stay с этим ключом за сегодняшний день отеля —
    тот же `service_day`, что у дневного номера `#N`, второй полуночи нет
    (P-12). Отменённые не в счёт: отмена норму возвращает; открытые — в счёт,
    иначе пять просьб подряд до первой доставки. Платная заявка сверх нормы и
    заявка другого Stay той же комнаты ключа квоты этого Stay не несут.

    Лога нет: исход проверки (в норме, частично, сверх) знает только
    вызывающий — его лог `service_quota_checked` (spec 0056 §4).
    """
    async with session_scope() as session:
        service_day = await _hotel_service_day(session)
        used = await session.scalar(
            select(func.coalesce(func.sum(ServiceRequest.quota_units), 0)).where(
                ServiceRequest.stay_id == stay_id,
                ServiceRequest.quota_key == rule.key,
                ServiceRequest.service_day == service_day,
                ServiceRequest.status != RequestStatus.CANCELLED,
            )
        )
    used = int(used or 0)
    limit = rule.per_guest_per_day * guests_count
    return QuotaCheck(limit=limit, used=used, free_units=min(units, max(limit - used, 0)))

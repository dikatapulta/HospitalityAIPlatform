"""Что бот знает о продлении проживания: свободные номера по ночам (spec 0059 §2.3).

Отель шлёт второй файл Opera — сколько номеров свободно на каждую ночь
ближайших недель; порт PMS отдаёт последний принятый (`fetch_availability`).
Здесь он сводится к тому, что нужно боту о Stay гостя: когда гость выезжает,
до какой ночи файл что-то знает и на какие из этих ночей мест нет.

`None` значит «бот ведёт себя как v1» (продление — заявкой или ресепшеном):
у отеля нет выгрузки Opera, Stay не активен, файла нет, он старше часа или не
покрывает ночь выезда. Свободен ли сам номер гостя, здесь не решается (§1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select

from hospitality.modules.guests.models import Stay, StayStatus
from hospitality.modules.guests.schemas import StayExtensionOutlook
from hospitality.platform.config import load_tenant_config
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.pms import get_pms
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Файл приходит раз в 15 минут (§2.1): старше часа — три-четыре задачи планировщика
# не дошли, и «места есть» могло уже стать неправдой.
AVAILABILITY_MAX_AGE: Final = timedelta(minutes=60)


async def get_extension_outlook(
    stay_id: uuid.UUID, *, now: datetime | None = None
) -> StayExtensionOutlook | None:
    """Свободные номера на ночи после выезда гостя — текущего тенанта; None — как v1.

    Активный Stay — как у гостевой сессии: `checked_in` и срок не вышел, поэтому
    дата выезда не раньше сегодняшней. Лог `stay_extension_outlook` — исход
    классом, без дат и комнаты.
    """
    moment = now if now is not None else utc_now()
    outlook, outcome = await _build_outlook(stay_id, moment)
    logger.info("stay_extension_outlook", outcome=outcome)
    return outlook


async def _build_outlook(
    stay_id: uuid.UUID, moment: datetime
) -> tuple[StayExtensionOutlook | None, str]:
    tenant_id = current_tenant_id()
    async with session_scope() as session:
        try:
            config = await load_tenant_config(session, tenant_id)
        except AppError as error:
            # Онбординг не завершён или конфиг дрейфнул: ход гостя идёт без блока.
            logger.warning("stay_extension_config_unavailable", error_code=error.code)
            return None, "config_unavailable"
        if config.pms_source != "opera_report":
            return None, "not_opera"
        check_out_at = await session.scalar(
            select(Stay.check_out_at).where(
                Stay.id == stay_id, Stay.status == StayStatus.CHECKED_IN
            )
        )
    if check_out_at is None or check_out_at <= moment:
        return None, "stay_inactive"
    availability = await get_pms().fetch_availability(tenant_id)
    if availability is None:
        return None, "no_file"
    if moment - availability.taken_at > AVAILABILITY_MAX_AGE:
        return None, "stale"
    zone = config.tzinfo
    local_checkout = check_out_at.astimezone(zone)
    departure = local_checkout.date()
    nights = availability.nights
    if not nights or not nights[0].night <= departure <= nights[-1].night:
        return None, "not_covered"
    full_nights = tuple(
        night.night for night in nights if night.night >= departure and night.free_rooms <= 0
    )
    outlook = StayExtensionOutlook(
        today=moment.astimezone(zone).date(),
        departure=departure,
        checkout_hour=local_checkout.hour,
        updated_at=availability.taken_at,
        known_through=nights[-1].night,
        full_nights=full_nights,
    )
    return outlook, "full_nights" if full_nights else "free"

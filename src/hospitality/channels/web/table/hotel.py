"""Включена ли бронь столика у отеля (spec 0064 §2.6) — кнопке чата и маршрутам гостя.

Отдельный файл, а не функция в `table/router.py`: страница чата (`web/router.py`)
спрашивает про кнопку, а `table/router.py` сам импортирует `web/router.py` —
круг импорта (образец `documents/hotel.py`).
"""

from __future__ import annotations

from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.table_booking_config import TableBookingConfig
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import current_tenant_id


async def table_booking_config() -> TableBookingConfig | None:
    """Бронь отеля, если она включена и в ней есть ресторан; иначе None.

    Зовётся внутри `tenant_context`. Тенант без конфига — None: брони у него нет
    (кнопки в чате нет, маршруты страницы — 404).
    """
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return None
    booking = config.table_booking
    if booking is None or not booking.is_open:
        return None
    return booking

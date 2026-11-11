"""Где работает фото к заявке и где лежат фото гостя (spec 0081 §4).

Отдельный модуль — чтобы и страница чата (`channels/web/router.py`: кнопка, адреса
фото в ленте), и маршруты фото спрашивали одно и то же без циклического импорта
(канон `documents/hotel.py`).
"""

from __future__ import annotations

from urllib.parse import quote

from hospitality.shared.config import get_settings


def guest_photos_enabled() -> bool:
    """Кнопка и маршруты фото: выключатель контура И заявки.

    В режиме «только консультации» (`ENABLE_SERVICE_REQUESTS=false`) заявок нет —
    фото некуда приложить, и кнопки нет.
    """
    settings = get_settings()
    return settings.enable_guest_request_photos and settings.enable_service_requests


def photo_base(tenant_slug: str, room_number: str) -> str | None:
    """Адрес фото гостя без id файла; None — фото выключены.

    Комната — из сессии: страница чужой комнаты всё равно уходит на свою (spec 0068).
    """
    if not guest_photos_enabled():
        return None
    return f"/g/{quote(tenant_slug, safe='')}/{quote(room_number, safe='')}/photos"

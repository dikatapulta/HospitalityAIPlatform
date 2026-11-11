"""Публичный интерфейс модуля concierge (spec 0085, R-5): справочник города.

Справочник — данные модуля, а не тенанта: отель его не правит, а подключает
флагом `TenantConfig.city_guide_enabled`; какой город — решает профиль отеля.
Текст блока для модели собирает AI-слой (`ai/city_guide.py`): модуль не знает,
как с ним говорит модель (слои FOUNDATION §5.1).
"""

from __future__ import annotations

from hospitality.modules.concierge.city_guide import (
    MAP_SEARCH_URL,
    MAX_CITY_GUIDE_ENTRIES,
    MAX_CITY_GUIDE_TEXT_LENGTH,
    MAX_CITY_GUIDE_TOPIC_LENGTH,
    MAX_CITY_GUIDE_TOTAL_TEXT_CHARS,
    CityGuide,
    CityGuideEntry,
    all_city_guides,
    city_guide_for,
)

__all__ = [
    "MAP_SEARCH_URL",
    "MAX_CITY_GUIDE_ENTRIES",
    "MAX_CITY_GUIDE_TEXT_LENGTH",
    "MAX_CITY_GUIDE_TOPIC_LENGTH",
    "MAX_CITY_GUIDE_TOTAL_TEXT_CHARS",
    "CityGuide",
    "CityGuideEntry",
    "all_city_guides",
    "city_guide_for",
]

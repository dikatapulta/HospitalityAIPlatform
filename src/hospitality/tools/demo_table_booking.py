"""Бронь столика демо-отеля (spec 0064 §6, PR 2): рестораны для staging и факт справочника.

Выдуманные рестораны — не рестораны пилота: названия и часы отеля приходят с его
файлом профиля `*.local` (§8 — вопросы к отелю). Все три отвечают службой `fnb`
демо-отеля — «Ресторан и room service». Казахские названия — черновик сессии.

Факт справочника (§2.5, ревью PR #711 Н-7) — данные, а не промпт: бот отвечает про
бронь фактом. «Проверит места и ответит» — та же формулировка, что под кнопкой
формы: «подтвердит» модель пересказала бы гостю как обещание брони.

Идемпотентно, как весь сид: конфиг дополняется, только если `table_booking` в нём
нет (правка руками переживает деплой); факт — если темы нет и справочник не полон.
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Final

from hospitality.platform.config import (
    MAX_HOTEL_FACTS,
    MAX_HOTEL_FACTS_TOTAL_CHARS,
    HotelFact,
    LocalizedText,
    TenantConfig,
    hotel_facts_total_chars,
    mutate_tenant_config,
)
from hospitality.platform.table_booking_config import RestaurantConfig, TableBookingConfig
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

DEMO_TABLE_BOOKING: Final = TableBookingConfig(
    enabled=True,
    restaurants=(
        RestaurantConfig(
            key="terrace",
            names=LocalizedText(en="Terrace", ru="Терраса", kk="Терраса"),
            note=LocalizedText(
                en="2nd floor, European cuisine",
                ru="2 этаж, европейская кухня",
                kk="2-қабат, еуропалық ас",
            ),
            category_key="fnb",
            first_seating=time(12, 0),
            last_seating=time(22, 0),
        ),
        RestaurantConfig(
            key="dastarkhan",
            names=LocalizedText(en="Dastarkhan", ru="Дастархан", kk="Дастарқан"),
            note=LocalizedText(
                en="Lobby floor, Kazakh cuisine",
                ru="Лобби, казахская кухня",
                kk="Лобби, қазақ асханасы",
            ),
            category_key="fnb",
            first_seating=time(11, 0),
            last_seating=time(23, 0),
            max_party=12,
        ),
        RestaurantConfig(
            key="sky-bar",
            names=LocalizedText(en="Sky Bar", ru="Скай-бар", kk="Скай-бар"),
            note=LocalizedText(
                en="Top floor, 18+", ru="Последний этаж, 18+", kk="Соңғы қабат, 18+"
            ),
            category_key="fnb",
            first_seating=time(18, 0),
            last_seating=time(23, 30),
            max_party=6,
        ),
    ),
)

DEMO_TABLE_BOOKING_FACT: Final = HotelFact(
    topic="Бронь столика в ресторане",
    answer=(
        "Столик в ресторанах отеля бронируют кнопкой «🍽 Забронировать столик» вверху чата: "
        "ресторан проверит места и ответит в чат."
    ),
)


async def seed_demo_table_booking(tenant_id: uuid.UUID) -> None:
    """Бронь столика демо-отеля: конфиг, если его нет, и факт справочника (идемпотентно)."""
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_demo_table_booking)


def _with_demo_table_booking(config: TenantConfig) -> TenantConfig:
    if config.table_booking is None:
        config = config.model_copy(update={"table_booking": DEMO_TABLE_BOOKING})
    topics = {fact.topic.strip().casefold() for fact in config.hotel_facts}
    if DEMO_TABLE_BOOKING_FACT.topic.casefold() in topics:
        return config
    facts = [*config.hotel_facts, DEMO_TABLE_BOOKING_FACT]
    if len(facts) > MAX_HOTEL_FACTS or hotel_facts_total_chars(facts) > MAX_HOTEL_FACTS_TOTAL_CHARS:
        # Справочник заполнен руками до предела — не ломать деплой ради демо-факта.
        logger.info("demo_table_booking_fact_skipped_full")
        return config
    return config.model_copy(update={"hotel_facts": facts})

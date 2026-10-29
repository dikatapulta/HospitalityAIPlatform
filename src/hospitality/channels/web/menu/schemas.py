"""Pydantic-схемы HTTP-границы страницы «Заказать в номер» (spec 0038 §4, R-6, P-7).

Проживание и идентичность гостя в теле не ходят — их берёт маршрут из сессии.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import LocalizedText


class MenuCatalog(BaseModel):
    """Каталог в виде гостя: открыт ли приём, сбор, заметка о доставке, разделы."""

    catalog: room_service_api.RoomServiceCatalog
    accepting_orders: bool
    service_charge_percent: Decimal
    delivery_note: LocalizedText | None
    sections: list[room_service_api.SectionWithItems]


class MenuData(BaseModel):
    """Данные страницы: комната из сессии, языки отеля, каталоги, открытые заказы."""

    room_number: str
    languages: list[str]
    catalogs: list[MenuCatalog]
    orders: list[room_service_api.OrderRead]


class OrderBody(BaseModel):
    """Тело «Оформить» (`POST …/orders`)."""

    model_config = ConfigDict(extra="forbid")

    catalog: room_service_api.RoomServiceCatalog
    client_order_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    language: room_service_api.OrderLanguage
    lines: list[room_service_api.CartLine] = Field(
        min_length=1, max_length=room_service_api.MAX_ORDER_LINES
    )
    comment: str | None = Field(default=None, max_length=room_service_api.MAX_COMMENT_LENGTH)
    expected_total_minor: int = Field(ge=0)

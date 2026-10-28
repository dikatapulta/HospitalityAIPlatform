"""Pydantic-схемы границ модуля room_service (spec 0038 §4, R-6, P-7) — копия
канона `modules/requests/schemas.py`: на входе `*Write`, наружу уходят `*Read`,
ORM-объекты — никогда.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from hospitality.modules.room_service.models import RoomServiceCatalog
from hospitality.platform.config import LocalizedText

# Предел названия раздела и товара — поле формы прототипа панели (spec 0038).
MAX_CATALOG_NAME_LENGTH: Final = 120
# Потолок цены — 1 000 000 ₸ в тиынах (spec 0038 Р-5). Колонка BIGINT
# переполнения не боится; потолок ловит опечатку «5 200 000» вместо «5 200»,
# пока гость не увидел её в корзине (LATER Н-6).
MAX_PRICE_MINOR: Final = 1_000_000 * 100
# Цена — целые тенге (Р-5): итог со сбором округляется до тенге, и дробная
# цена дала бы итог, который нельзя ни показать, ни принять наличными.
MINOR_UNITS_PER_TENGE: Final = 100


def _names_fit(value: LocalizedText) -> LocalizedText:
    too_long = [
        language
        for language, text in value.as_json().items()
        if len(text) > MAX_CATALOG_NAME_LENGTH
    ]
    if too_long:
        raise ValueError(f"name is longer than {MAX_CATALOG_NAME_LENGTH} characters: {too_long}")
    return value


CatalogName = Annotated[LocalizedText, AfterValidator(_names_fit)]


class MoveDirection(enum.StrEnum):
    """Стрелки «Выше» / «Ниже» у раздела и товара в панели."""

    UP = "up"
    DOWN = "down"


class SectionWrite(BaseModel):
    """Раздел: новый или переименованный."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    names: CatalogName


class ItemWrite(BaseModel):
    """Товар целиком — форма «Новый товар» / «Изменить товар» панели.

    Скрытие из меню — не поле формы, а отдельное действие (`set_hidden`):
    сохранение формы не должно молча вернуть скрытый товар гостю.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: uuid.UUID
    names: CatalogName
    descriptions: LocalizedText | None = None
    price_minor: int = Field(gt=0, le=MAX_PRICE_MINOR, multiple_of=MINOR_UNITS_PER_TENGE)
    is_available: bool = True


class SectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    catalog: RoomServiceCatalog
    position: int
    names: LocalizedText


class ItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    section_id: uuid.UUID
    position: int
    names: LocalizedText
    descriptions: LocalizedText | None
    price_minor: int
    is_available: bool
    hidden_at: datetime | None


class SectionWithItems(SectionRead):
    items: list[ItemRead]


class CatalogRead(BaseModel):
    """Каталог по порядку разделов и товаров (вид гостя или панели — `get_catalog`)."""

    catalog: RoomServiceCatalog
    sections: list[SectionWithItems]


class CatalogSettingsRead(BaseModel):
    """Настройки каталога для панели и страницы гостя: «принимаем» из БД модуля,
    служба, сбор и заметка о доставке — из конфига тенанта (Р-10)."""

    catalog: RoomServiceCatalog
    accepting_orders: bool
    category_key: str
    service_charge_percent: Decimal
    delivery_note: LocalizedText | None

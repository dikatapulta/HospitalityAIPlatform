"""Pydantic-схемы границ модуля room_service (spec 0038 §4, R-6, P-7) — копия
канона `modules/requests/schemas.py`: на входе `*Write`, наружу уходят `*Read`,
ORM-объекты — никогда.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from hospitality.modules.room_service.models import OrderCancelReason, RoomServiceCatalog
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


# --- Заказы (PR 5 серии, spec 0038 §4) ---

# Пределы заказа (spec 0038 §3–§4): позиций в корзине, штук одной позиции,
# символов пожеланий; у проживания — открытых заказов и заказов за сутки отеля
# (сутки — как у дневных номеров заявок, ревью PR #497 Н-10).
MAX_ORDER_LINES: Final = 30
MAX_LINE_QUANTITY: Final = 20
MAX_COMMENT_LENGTH: Final = 200
MAX_OPEN_ORDERS_PER_STAY: Final = 3
MAX_ORDERS_PER_HOTEL_DAY: Final = 10
# Языки заказа — языки страницы гостя (Р-6).
OrderLanguage = Literal["en", "ru", "kk"]


class OrderStage(enum.StrEnum):
    """Стадия заказа для гостя — выводится из заявки (Р-4, `order_stage`)."""

    SENT = "sent"
    ACCEPTED = "accepted"
    PAID = "paid"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class CartLine(BaseModel):
    """Строка корзины: товар и сколько штук."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: uuid.UUID
    quantity: int = Field(ge=1, le=MAX_LINE_QUANTITY)


def _distinct_items(lines: list[CartLine]) -> list[CartLine]:
    seen = {line.item_id for line in lines}
    if len(seen) != len(lines):
        raise ValueError("each item may appear in the cart only once")
    return lines


CartLines = Annotated[
    list[CartLine],
    Field(min_length=1, max_length=MAX_ORDER_LINES),
    AfterValidator(_distinct_items),
]


class QuoteRequest(BaseModel):
    """Корзина для пересчёта итога сервером (`quote`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog: RoomServiceCatalog
    lines: CartLines


class OrderPlace(BaseModel):
    """Заказ гостя — вход `place_order`.

    Проживание и идентичность — из гостевой сессии, не из тела запроса (канал
    подставляет их сам). `expected_total_minor` — итог, который гость видел:
    разошёлся с пересчётом — ERR-ROOMSERVICE-005 (Р-5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog: RoomServiceCatalog
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    client_order_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    guest_language: OrderLanguage
    lines: CartLines
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_LENGTH)
    expected_total_minor: int = Field(ge=0)

    @field_validator("comment")
    @classmethod
    def _blank_comment_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class OrderLineRead(BaseModel):
    """Позиция заказа или пересчёта: снимок названия и цены."""

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    names: LocalizedText
    unit_price_minor: int
    quantity: int
    line_total_minor: int


class Quote(BaseModel):
    """Итог корзины, посчитанный сервером (Р-5)."""

    catalog: RoomServiceCatalog
    currency: str
    lines: list[OrderLineRead]
    subtotal_minor: int
    service_charge_percent: Decimal
    service_charge_minor: int
    total_minor: int


class OrderRead(BaseModel):
    """Заказ для гостя и кабинета: суммы модуля плюс номер, шаги и время — из заявки."""

    id: uuid.UUID
    catalog: RoomServiceCatalog
    request_id: uuid.UUID
    number: int | None
    stage: OrderStage
    room_number: str | None
    guest_language: str
    currency: str
    lines: list[OrderLineRead]
    subtotal_minor: int
    service_charge_percent: Decimal
    service_charge_minor: int
    total_minor: int
    comment: str | None
    cancel_reason: OrderCancelReason | None
    created_at: datetime
    accepted_at: datetime | None
    paid_at: datetime | None
    closed_at: datetime | None


class OrderPlaced(BaseModel):
    """Итог «Оформить»: заказ и признак повтора тем же ключом (200, а не 201)."""

    order: OrderRead
    duplicate: bool


def service_charge_minor(subtotal_minor: int, percent: Decimal) -> int:
    """Сбор в тиынах: сумма × процент / 100, до целого тенге, половина вверх (Р-5)."""
    tenge = Decimal(subtotal_minor) / MINOR_UNITS_PER_TENGE * percent / 100
    return int(tenge.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * MINOR_UNITS_PER_TENGE


def format_amount(amount_minor: int, language: str) -> str:
    """Деньги словами гостя (spec 0038 §5): `5 720 ₸` (ru, kk), `5,720 ₸` (en)."""
    grouped = f"{amount_minor // MINOR_UNITS_PER_TENGE:,}"
    return (grouped if language == "en" else grouped.replace(",", " ")) + " ₸"


def format_percent(percent: Decimal) -> str:
    """Процент сбора без хвостовых нулей: `10`, `12.5`.

    Конфиг хранит процент как прислали (`"1E+1"` проходит схему — ревью PR #508,
    П-12): гость видит нормальное число, а не экспоненту.
    """
    text = format(percent.quantize(Decimal("0.01")), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text

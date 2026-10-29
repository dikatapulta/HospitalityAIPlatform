"""ORM-модели модуля room_service: каталоги (spec 0038 §3, PR 3) и заказы (PR 5).

Таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграциях 0041 и 0044 (копия канона 0002).
FK в чужие модули нет (образец `request_origins`): заказ знает заявку, проживание
и гостя по id. FK внутри модуля есть: товар → раздел, позиция → заказ и товар;
в ядро — фото товара → `uploaded_files` (миграция 0045).
Не удаляется ничего из каталога: позиции заказов ссылаются на товар, поэтому
«убрать из меню» — обратимое `hidden_at`, а пустой раздел гость просто не видит.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class RoomServiceCatalog(enum.StrEnum):
    """Каталог room service (spec 0038 Р-1): ресторан и магазин отеля.

    Значения — те же, что ключи `RoomServiceConfig.catalogs` в конфиге тенанта
    (`platform/config.py`, `RoomServiceCatalogKey`); расхождение ловит тест.
    """

    RESTAURANT = "restaurant"
    SHOP = "shop"


# Канон `request_status_column_type`: VARCHAR со значениями `.value`, состав
# значений меняется миграцией данных, а не ALTER TYPE.
catalog_column_type = Enum(
    RoomServiceCatalog,
    name="room_service_catalog",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class RoomServiceSection(Base):
    """Раздел каталога («Завтраки», «Гигиена») — заголовок над товарами у гостя."""

    __tablename__ = "room_service_sections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    catalog: Mapped[RoomServiceCatalog] = mapped_column(catalog_column_type)
    # Порядок у гостя и в панели; одинаковые позиции допустимы (гонка двух
    # «Добавить раздел») — порядок тогда держит `created_at`, а первая же
    # перестановка перенумерует раздел заново (`service._renumber`).
    position: Mapped[int] = mapped_column(Integer())
    # `LocalizedText.as_json()`: {"en": …, "ru": …, "kk": …}.
    names: Mapped[dict[str, str]] = mapped_column(JSONB())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class RoomServiceItem(Base):
    """Товар каталога: блюдо ресторана или товар магазина.

    Стабильный UUID — под будущий POS (NG-8) и под позиции заказа (PR 5),
    которые хранят снимок названия и цены.
    """

    __tablename__ = "room_service_items"
    __table_args__ = (CheckConstraint("price_minor > 0", name="price_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("room_service_sections.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer())
    names: Mapped[dict[str, str]] = mapped_column(JSONB())
    descriptions: Mapped[dict[str, str] | None] = mapped_column(JSONB())
    # Тиын (spec 0038 Р-5). BIGINT, а не INTEGER: итог заказа (30 позиций по
    # 20 штук) на INTEGER переполнился бы от одной опечатки в цене (LATER Н-6).
    price_minor: Mapped[int] = mapped_column(BigInteger())
    # «В наличии / Закончилось»: гость видит товар, но в корзину не положит.
    is_available: Mapped[bool] = mapped_column(Boolean(), default=True)
    # «Скрыт из меню»: гость товара не видит; обратимо.
    hidden_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Фото (PR 7, миграция 0045) — строка `uploaded_files` ядра, назначение
    # `menu_photo`. UNIQUE: файл принадлежит одному товару, и замена удаляет
    # прежний, не задевая соседа; файл удалили — товар без фото (SET NULL).
    photo_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="SET NULL"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class RoomServiceSettings(Base):
    """«Принимаем заказы» каталога — переключатель служб, а не настройка оператора.

    Строки нет — «не принимаем»: каталог, который служба ещё не открыла, не
    берёт заказов. Сбор и служба каталога — в конфиге тенанта (Р-10).
    """

    __tablename__ = "room_service_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True, default=current_tenant_id
    )
    catalog: Mapped[RoomServiceCatalog] = mapped_column(catalog_column_type, primary_key=True)
    accepting_orders: Mapped[bool] = mapped_column(Boolean(), default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class OrderCancelReason(enum.StrEnum):
    """Почему заказ отменён (spec 0038 §4–§5): гость сам или причина сотрудника.

    `BY_GUEST` — отмена гостем на странице, пока заказ «Отправлен»: в чат о ней
    не пишется. Остальные — выбор сотрудника; гость читает их своими словами
    (тексты — в канале). Отмены без причины (общая кнопка очереди, API) гость
    читает как «Другое» (ревью PR #497, Н-8).
    """

    BY_GUEST = "by_guest"
    NOT_ORDERED = "not_ordered"
    NO_ANSWER = "no_answer"
    PAYMENT_REFUSED = "payment_refused"
    ITEM_UNAVAILABLE = "item_unavailable"
    OTHER = "other"


cancel_reason_column_type = Enum(
    OrderCancelReason,
    name="room_service_cancel_reason",
    native_enum=False,
    length=32,
    values_callable=lambda members: [member.value for member in members],
)


class RoomServiceOrder(Base):
    """Заказ гостя в номер (spec 0038 Р-1, Р-3): корзина, суммы и ссылка на заявку.

    Жизненный цикл — у заявки (`request_id`): номер `#N`, комната, пожелания,
    шаги и времена живут в ней, стадия выводится из неё (`order_stage`).
    `request_id` выбирается до заявки (Р-3), `request_created` отмечает, что
    заявка есть: заказ без неё старше суток удаляет ретеншн.

    `stay_id` и `guest_identity_id` — связь с гостем: через 90 дней ретеншн
    обнуляет обе (spec 0032, ревью PR #497 Н-2), состав и суммы остаются
    отчётам отеля. Отсюда и nullable.
    """

    __tablename__ = "room_service_orders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "stay_id", "client_order_key", name="uq_room_service_orders_client_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    catalog: Mapped[RoomServiceCatalog] = mapped_column(catalog_column_type)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(), unique=True)
    request_created: Mapped[bool] = mapped_column(Boolean(), default=False, server_default=false())
    stay_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), index=True)
    guest_identity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # Ключ кнопки «Оформить» на странице: двойное нажатие — один заказ.
    client_order_key: Mapped[str] = mapped_column(String(64))
    guest_language: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    # Суммы в тиынах BIGINT (ревью PR #497, Н-6): 30 позиций × 20 штук по
    # потолку цены не переполнят колонку.
    subtotal_minor: Mapped[int] = mapped_column(BigInteger())
    service_charge_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    service_charge_minor: Mapped[int] = mapped_column(BigInteger())
    total_minor: Mapped[int] = mapped_column(BigInteger())
    cancel_reason: Mapped[OrderCancelReason | None] = mapped_column(cancel_reason_column_type)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class RoomServiceOrderLine(Base):
    """Позиция заказа — снимок названия и цены на момент заказа (Р-5)."""

    __tablename__ = "room_service_order_lines"
    __table_args__ = (
        CheckConstraint("quantity BETWEEN 1 AND 20", name="quantity_range"),
        CheckConstraint("unit_price_minor > 0", name="unit_price_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("room_service_orders.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("room_service_items.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer())
    names: Mapped[dict[str, str]] = mapped_column(JSONB())
    unit_price_minor: Mapped[int] = mapped_column(BigInteger())
    quantity: Mapped[int] = mapped_column(Integer())
    line_total_minor: Mapped[int] = mapped_column(BigInteger())

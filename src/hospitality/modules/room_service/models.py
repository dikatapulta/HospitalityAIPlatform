"""ORM-модели модуля room_service: каталоги (spec 0038 §3, PR 3 серии).

Таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0041 (копия канона 0002).
FK в чужие модули нет (образец `request_origins`); FK товара на свой раздел —
внутри модуля. Не удаляется ничего: заказы (PR 5) ссылаются на товар, поэтому
«убрать из меню» — обратимое `hidden_at`, а пустой раздел гость просто не видит.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
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

"""ORM-модели модуля platform (Task 0008, FOUNDATION §9, ADR-003).

`Tenant` — корень мультитенантности: единица изоляции данных и конфигурации
(GLOSSARY: «Тенант»). Сама таблица `tenants` — НЕ тенантная (это реестр
тенантов), поэтому `tenant_id` и RLS на ней нет; RLS-канон для тенантных
таблиц появится в Task 0009.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # slug — стабильный человекочитаемый идентификатор (URL, конфиги, сиды);
    # name — отображаемое название, может меняться свободно.
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

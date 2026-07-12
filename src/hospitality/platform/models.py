"""ORM-модели модуля platform (Task 0008/0009, FOUNDATION §9, ADR-003).

`Tenant` — корень мультитенантности: единица изоляции данных и конфигурации
(GLOSSARY: «Тенант»). Сама таблица `tenants` — НЕ тенантная (это реестр
тенантов), поэтому `tenant_id` и RLS на ней нет.

`TenantIsolationCanary` — канонический образец тенантной таблицы (Task 0009):
новые тенантные модели копируют её паттерн, а миграции — RLS-блок из
`alembic/versions/0002_tenant_rls_canon.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # slug — стабильный человекочитаемый идентификатор (URL, конфиги, сиды);
    # name — отображаемое название, может меняться свободно.
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    # Конфигурация тенанта (§6, Task 0011): форму задаёт схема TenantConfig,
    # читать/писать только через load_tenant_config/store_tenant_config
    # (platform/config.py). NULL = онбординг не завершён.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class TenantIsolationCanary(Base):
    """CANONICAL: образец тенантной таблицы (Task 0009, P-4, ADR-003).

    Вечный якорь обязательного теста изоляции (`tests/test_tenant_isolation.py`,
    отдельный блокирующий шаг CI) и образец для копирования в каждую новую
    тенантную таблицу. В проде пуста — бизнес-данных не несёт.

    Канон тенантной модели:
    - `tenant_id` NOT NULL с FK на `tenants.id` и индексом;
    - default берёт тенанта из `tenant_context` — забыть проставить нельзя,
      а подлог чужого tenant_id всё равно отвергает RLS-политика (WITH CHECK);
    - в миграции таблица получает RLS-блок (ENABLE + FORCE + политика) —
      см. канонический комментарий в миграции 0002.
    """

    __tablename__ = "tenant_isolation_canary"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    note: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

"""ORM-модель модуля showroom: сцена 3D-витрины (spec 0069 §3, PR а).

Таблица тенантная: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0059 (копия канона 0002). Одна
строка на тенанта (`tenant_id` UNIQUE): новая сцена заменяет прежнюю, версия растёт.
ПД нет: сцена — здание без имён и занятости. FK в ядро — кто заменил (`users`,
ON DELETE SET NULL).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class ShowroomScene(Base):
    """Сцена витрины отеля — документ `SceneDocument` в JSONB (§2.1)."""

    __tablename__ = "showroom_scenes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, default=current_tenant_id
    )
    scene: Mapped[dict[str, Any]] = mapped_column(JSONB())
    scene_version: Mapped[int] = mapped_column(Integer())
    replaced_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    replaced_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

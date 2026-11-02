"""Таблица адаптера Opera: токен загрузки и последние принятые файлы (spec 0058 §3).

Тенантная таблица по канону `TenantIsolationCanary` (P-4, ADR-003): RLS — в
миграции 0052, колонки свободных номеров (spec 0059 §3) — в 0053; `tenant_id`
по умолчанию — из `tenant_context`. Своя у адаптера,
а не у `modules/guests`: снимок — это то, что прислал отель, а не состояние
Stay; сверка читает его только через порт (`fetch_snapshot`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime
from hospitality.shared.tenancy import current_tenant_id


class PmsReportFeed(Base):
    """Строка на тенанта: хэш токена загрузки и последний принятый файл каждого вида.

    `source_id`, `taken_at`, `content_sha256` пусты, пока файлов не было
    (`fetch_snapshot` отдаёт `None`). `rows` — строки снимка массивом
    объектов `{room, booking_ref, arrival, departure, guests}`; заменяется
    целиком каждым принятым файлом.

    `availability_*` — второй файл, свободные номера по ночам (spec 0059 §2.1):
    `availability_taken_at` пусто, пока его не было (`fetch_availability` —
    `None`); `availability_nights` — массив `{night, free_rooms}` по возрастанию.
    Со снимком не связан: один файл принимается без другого.
    """

    __tablename__ = "pms_report_feeds"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True, default=current_tenant_id
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    rows: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), default=list, server_default=sql_text("'[]'::jsonb")
    )
    availability_taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    availability_sha256: Mapped[str | None] = mapped_column(String(64))
    availability_nights: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), default=list, server_default=sql_text("'[]'::jsonb")
    )

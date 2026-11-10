"""ORM-модель модуля feedback: оценка гостем выполненной заявки (spec 0076 §4.1).

Таблица тенантная: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0068 (копия канона 0002).
FK в чужие модули нет (образец `table_bookings`): оценка знает заявку, её службу
и карточку пальца вниз по id.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class FeedbackRating(enum.StrEnum):
    """Оценка гостя (§2): палец вверх или вниз — без звёзд (§11)."""

    UP = "up"
    DOWN = "down"


# Канон `request_status`: VARCHAR со значениями `.value`, а не тип Postgres.
rating_column_type = Enum(
    FeedbackRating,
    name="feedback_rating",
    native_enum=False,
    length=8,
    values_callable=lambda members: [member.value for member in members],
)


class RequestFeedback(Base):
    """Одна оценка на заявку (§4.1): `(tenant_id, request_id)` уникален.

    `category_id` — служба заявки на момент оценки: сводка считает по ней без
    чужого SQL (P-2). `comment` — текст гостя, срок у spec 0032: ретеншн
    обнуляет его по `comment_at`, оценка и служба остаются. `card_request_id` —
    карточка пальца вниз в службе эскалаций; NULL — карточки нет (оценка `up`
    или шаг карточки не удался).
    """

    __tablename__ = "request_feedback"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "request_id", name="uq_request_feedback_tenant_id_request_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    rating: Mapped[FeedbackRating] = mapped_column(rating_column_type)
    comment: Mapped[str | None] = mapped_column(Text())
    comment_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    card_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

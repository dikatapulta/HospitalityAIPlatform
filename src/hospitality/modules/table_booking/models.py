"""ORM-модель модуля table_booking: бронь столика (spec 0064 §3, PR 2).

Таблица тенантная: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0057 (копия канона 0002).
FK в чужие модули нет (образец `room_service_orders`): бронь знает заявку,
проживание и гостя по id. FK в ядро — кто ответил (`users`, ON DELETE SET NULL).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class BookingAnswer(enum.StrEnum):
    """Ответ ресторана (§2.3). Нет ответа — NULL в колонке: ждём."""

    CONFIRMED = "confirmed"
    OTHER_TIME = "other_time"
    NO_SEATS = "no_seats"


answer_column_type = Enum(
    BookingAnswer,
    name="table_booking_answer",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class TableBooking(Base):
    """Запрос гостя на столик и ответ ресторана (spec 0064 §2.1, §3).

    Жизненный цикл — у заявки (`request_id`): номер `#N`, очередь, лестница
    невзятой заявки. `request_id` выбирается до заявки (Р-3 spec 0038),
    `request_created` отмечает, что заявка есть: бронь без неё старше суток
    удаляет ретеншн. Имя и пожелания — `details` заявки (обезличиваются с ней).

    `cancel_request_id` — заявка «Гость отменил бронь» после «Подтвердить»
    (§2.4): id пишется вместе с отметкой отмены, `cancel_request_created` — что
    она создана (ревью PR #711, Н-4).

    `stay_id` и `guest_identity_id` — связь с гостем: ретеншн обнуляет обе в
    срок spec 0032, ресторан, час и число гостей остаются. Отсюда и nullable.
    """

    __tablename__ = "table_bookings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "stay_id", "client_key", name="uq_table_bookings_client_key"),
        CheckConstraint("party_size BETWEEN 1 AND 30", name="party_size_range"),
        CheckConstraint(
            "(answer IS NOT DISTINCT FROM 'other_time') = (proposed_at IS NOT NULL)",
            name="proposed_at_only_for_other_time",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(), unique=True)
    request_created: Mapped[bool] = mapped_column(Boolean(), default=False, server_default=false())
    stay_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), index=True)
    guest_identity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # Ключ кнопки «Отправить запрос» на странице: двойное нажатие — один запрос.
    client_key: Mapped[str] = mapped_column(String(64))
    restaurant_key: Mapped[str] = mapped_column(String(32))
    # Снимок названий: ресторан переименуют или уберут из конфига, а бронь живёт.
    restaurant_names: Mapped[dict[str, str]] = mapped_column(JSONB())
    booking_at: Mapped[datetime] = mapped_column(UTCDateTime())
    party_size: Mapped[int] = mapped_column(Integer())
    guest_language: Mapped[str] = mapped_column(String(2))
    answer: Mapped[BookingAnswer | None] = mapped_column(answer_column_type)
    proposed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    answered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    answered_by_display_name: Mapped[str | None] = mapped_column(String(255))
    cancelled_by_guest_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), unique=True)
    cancel_request_created: Mapped[bool] = mapped_column(
        Boolean(), default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

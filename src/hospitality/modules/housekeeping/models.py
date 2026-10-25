"""ORM-модель модуля housekeeping (spec 0046 §3, ADR-021).

Таблица тенантная: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0035 (копия канона 0002).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class TurnoverStatus(enum.StrEnum):
    """Путь номера после выезда (spec 0046 §3): строго по порядку, отменить — любой открытый.

    Допустимые переходы — `TURNOVER_TRANSITIONS` в `service.py`: статус меняется
    только через `change_turnover_status`, прямые UPDATE статуса запрещены.
    """

    REPORTED = "reported"
    INSPECTED = "inspected"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    READY = "ready"
    CANCELLED = "cancelled"


# Канон `request_status_column_type`: VARCHAR со значениями `.value`, состав
# значений меняется миграцией данных, а не ALTER TYPE.
turnover_status_column_type = Enum(
    TurnoverStatus,
    name="turnover_status",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)

# Открытые статусы — для partial UNIQUE «один открытый путь на номер». Текст
# условия тот же в миграции 0035: меняется карта — меняются оба места.
OPEN_TURNOVER_STATUSES_SQL = "status IN ('reported', 'inspected', 'cleaning', 'cleaned')"


class RoomTurnover(Base):
    """Уборка после выезда (GLOSSARY): путь одного номера от выезда до «готов к заселению».

    Кабинет ведёт работу, а не статус номера — тот остаётся в OPERA (NG-2).
    «Кто» на каждом шаге — пара «FK на `users` + снапшот имени», как
    `claimed_by_*` у заявки: доска не ходит в платформенные таблицы, а
    переименование сотрудника не переписывает историю.
    """

    __tablename__ = "room_turnovers"
    # Один ОТКРЫТЫЙ путь на номер — свойство БД (канон `uq_stays_tenant_room_checked_in`):
    # две одновременные отметки выезда одного номера не создадут двух путей.
    __table_args__ = (
        Index(
            "uq_room_turnovers_tenant_room_open",
            "tenant_id",
            "room_number",
            unique=True,
            postgresql_where=OPEN_TURNOVER_STATUSES_SQL,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    room_number: Mapped[str] = mapped_column(String(20))
    status: Mapped[TurnoverStatus] = mapped_column(
        turnover_status_column_type, default=TurnoverStatus.REPORTED
    )
    # Замечание хаускипинга при проверке (минибар, поломка): ресепшен видит его,
    # пока гость у стойки. Через 90 дней — NULL джобой ретеншна (spec 0046 §8).
    inspection_note: Mapped[str | None] = mapped_column(String(500))
    reported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reported_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reported_by_display_name: Mapped[str | None] = mapped_column(String(255))
    inspected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    inspected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    inspected_by_display_name: Mapped[str | None] = mapped_column(String(255))
    cleaning_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cleaning_started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    cleaning_started_by_display_name: Mapped[str | None] = mapped_column(String(255))
    cleaned_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cleaned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    cleaned_by_display_name: Mapped[str | None] = mapped_column(String(255))
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    accepted_by_display_name: Mapped[str | None] = mapped_column(String(255))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    cancelled_by_display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

"""ORM-модель запроса из 3D-витрины (spec 0069 §2.4, §3; PR (в) серии, #805).

Таблица тенантная: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0060 (копия канона 0002). FK в
чужие модули нет (образец `table_bookings`): запрос знает заявку по id.

Контакты посетителя живут только здесь, а не в тексте заявки: у них один срок —
365 дней (§3), а не два. Обезличивание обнуляет контакты, компанию и пожелания и
ставит `anonymized_at`; виды, даты, гостей и номера остаются — для отчётов.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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


class ShowroomRequestKind(enum.StrEnum):
    """Вид запроса (§2.4): вкладка «Для групп» или «Для гостей»."""

    GROUP = "group"
    BOOKING = "booking"


kind_column_type = Enum(
    ShowroomRequestKind,
    name="showroom_request_kind",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class ShowroomRequest(Base):
    """Запрос посетителя витрины и его контакты (spec 0069 §3).

    Жизненный цикл — у заявки (`request_id`, выбирается до заявки, Р-3 spec 0038):
    номер `#N`, очередь службы, сигнал, лестница. `request_created` отмечает, что
    заявка есть: строку без неё старше суток удаляет ретеншн (как у брони столика).

    Названия желаемого вида и типа номера — снимком (`*_names`, канон
    `table_bookings.restaurant_names`): отель заменит сцену, а запрос живёт.
    """

    __tablename__ = "showroom_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_key", name="uq_showroom_requests_client_key"),
        CheckConstraint("guests_count BETWEEN 1 AND 500", name="guests_count_range"),
        CheckConstraint("check_out > check_in", name="check_out_after_check_in"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(), unique=True)
    request_created: Mapped[bool] = mapped_column(Boolean(), default=False, server_default=false())
    # Ключ кнопки «Отправить запрос»: двойное нажатие — один запрос.
    client_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[ShowroomRequestKind] = mapped_column(kind_column_type)
    # Контакты, компания и пожелания — NULL после обезличивания (365 дней, §3).
    company: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    guests_count: Mapped[int] = mapped_column(Integer())
    check_in: Mapped[date] = mapped_column(Date())
    check_out: Mapped[date] = mapped_column(Date())
    room_numbers: Mapped[list[str]] = mapped_column(JSONB())
    wish_view_key: Mapped[str | None] = mapped_column(String(64))
    wish_view_names: Mapped[dict[str, str] | None] = mapped_column(JSONB())
    wish_type_key: Mapped[str | None] = mapped_column(String(64))
    wish_type_names: Mapped[dict[str, str] | None] = mapped_column(JSONB())
    wishes: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(2))
    consent_version: Mapped[str] = mapped_column(String(32))
    consent_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

"""ORM-модели модуля requests (Task 0012, FOUNDATION §5.2, GLOSSARY).

Обе таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`, Task 0009), RLS-блок — в миграции 0006 (копия канона
0002). `tenant_id` берётся из `tenant_context` по умолчанию, подлог чужого
tenant_id отвергает RLS-политика (WITH CHECK).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class RequestStatus(enum.StrEnum):
    """Жизненный цикл заявки (§5.2): new → assigned → in_progress → done/cancelled.

    Допустимые переходы — `STATUS_TRANSITIONS` в `service.py`: статус меняется
    только через `change_request_status`, прямые UPDATE статуса запрещены.
    """

    NEW = "new"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


# Единственное место истины для колонки status: значения — .value членов enum
# (по умолчанию SQLAlchemy пишет ИМЕНА членов — "NEW", а не "new").
# native_enum=False — обычный VARCHAR вместо типа Postgres: изменение состава
# значений остаётся обычной миграцией данных, а не ALTER TYPE.
request_status_column_type = Enum(
    RequestStatus,
    name="request_status",
    native_enum=False,
    length=32,
    values_callable=lambda members: [member.value for member in members],
)


class RequestCategory(Base):
    """Категория заявки: конфигурируемый тип (GLOSSARY) — уборка, ремонт, IT…

    Новый тип заявки = строка в этой таблице у тенанта, а не новый модуль
    (ключевое решение §5.2). Маршрутизация в службу, SLA и кастомные поля
    категории появятся в Phase 1 — добавлением колонок, не сменой модели.
    """

    __tablename__ = "request_categories"
    # `key` уникален в пределах тенанта, не глобально: у каждого отеля свой
    # набор категорий с одинаковыми типовыми ключами ("housekeeping" и т.п.).
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    # key — стабильный идентификатор категории (конфиги, AI-инструменты, сиды);
    # name — отображаемое название, может меняться свободно (паттерн Tenant.slug).
    key: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class ServiceRequest(Base):
    """Заявка: единица работы для службы отеля (GLOSSARY: «Заявка»).

    Один жизненный цикл для всех категорий; создание и смена статуса — только
    через `service.py` (там же публикуются события `request.created` /
    `request.status_changed` в той же транзакции, P-6).
    """

    __tablename__ = "service_requests"
    # Дневной номер `#N` уникален в паре (тенант, день отеля): этот же индекс —
    # защита от гонки присвоения (второй INSERT с занятым номером отвергается,
    # service.create_request пересчитывает и повторяет). Явное имя — оно же
    # опознаётся в тексте IntegrityError сервисом. Разные дни повторяют `#12`:
    # номер — метка, не ключ (issue #38, заход 2а).
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "service_day", "daily_number", name="uq_service_requests_daily_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("request_categories.id"), index=True)
    status: Mapped[RequestStatus] = mapped_column(
        request_status_column_type, default=RequestStatus.NEW
    )
    # summary — короткая суть для списков и уведомлений; details — свободный
    # текст (полное пожелание гостя). Привязка к гостю/номеру появится с
    # модулями guests/stays (Phase 1) — отдельной колонкой, не переделкой.
    summary: Mapped[str] = mapped_column(String(500))
    details: Mapped[str | None] = mapped_column(Text())
    room_number: Mapped[str | None] = mapped_column(String(20))
    # Дневной номер `#N` и день отеля, за который он присвоен (локальная дата из
    # tz тенанта, §9). NULLABLE: строки до миграции 0010 номера не имеют. Новые
    # заявки всегда получают оба поля в service.create_request.
    service_day: Mapped[date | None] = mapped_column(Date())
    daily_number: Mapped[int | None] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

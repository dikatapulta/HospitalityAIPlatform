"""ORM-модели модуля documents (spec 0049 §3).

Таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0042 (копия канона 0002).
Реквизиты и текст гостя — только шифртекстом (FOUNDATION §11, Р-3): открытые
колонки — номер, даты, состояния и имена полей, ни одного значения.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class ExtractionState(enum.StrEnum):
    """Разбор запроса (Р-4): `failed` — хотя бы один PDF или DOCX не разобран."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class FileTextState(enum.StrEnum):
    """Текст одного файла: `image` — фото, поля заполняет человек; `vision` — фото
    прочитала модель, черновик полей — в строке (Р-13). Колонка — VARCHAR без CHECK:
    новое значение миграции не требует.

    `text`, `no_text`, `failed` — значения `platform.file_text.TextState`.
    """

    PENDING = "pending"
    TEXT = "text"
    NO_TEXT = "no_text"
    IMAGE = "image"
    VISION = "vision"
    FAILED = "failed"


# Канон `request_status_column_type`: VARCHAR со значениями `.value`.
extraction_state_column_type = Enum(
    ExtractionState,
    name="act_extraction_state",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
file_text_state_column_type = Enum(
    FileTextState,
    name="act_file_text_state",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class ActRequest(Base):
    """Запрос реквизитов для АВР (GLOSSARY): форма гостя → строка таблицы бухгалтерии.

    Живёт циклом своей заявки (`request_id`, Р-1) — статус, `#N` и «Готово» у
    неё. `request_id` без FK (spec 0038 Р-3): строка пишется раньше заявки.
    `edited_fields` — поля, которые правил человек (Р-4, Н-10 ревью PR #500):
    повторный разбор их не трогает. `request_created` — заявка точно создана;
    false старше суток — брошенный запрос, ретеншн сверяет и удаляет (§3).
    """

    __tablename__ = "act_requests"
    __table_args__ = (
        # Один `client_key` — один запрос проживания: повтор отправки и гонка (Р-8).
        UniqueConstraint(
            "tenant_id", "stay_id", "client_key", name="uq_act_requests_stay_client_key"
        ),
        Index("ix_act_requests_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(), unique=True)
    request_created: Mapped[bool] = mapped_column(Boolean(), default=False)
    stay_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    guest_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    client_key: Mapped[str] = mapped_column(String(64))
    guest_language: Mapped[str] = mapped_column(String(2))
    room_number: Mapped[str] = mapped_column(String(20))
    check_in_at: Mapped[datetime] = mapped_column(UTCDateTime())
    check_out_at: Mapped[datetime] = mapped_column(UTCDateTime())
    guest_input_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    requisites_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    edited_fields: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    extraction_state: Mapped[ExtractionState] = mapped_column(
        extraction_state_column_type, default=ExtractionState.PENDING
    )
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    checked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    checked_by_display_name: Mapped[str | None] = mapped_column(String(255))
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ActRequestFile(Base):
    """Файл запроса: связь с `uploaded_files` и состояние его текста.

    `file_id` ON DELETE CASCADE: джоба ретеншна узла удаляет файл — уходит и связь.
    """

    __tablename__ = "act_request_files"
    __table_args__ = (
        UniqueConstraint(
            "act_request_id", "position", name="uq_act_request_files_request_position"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    act_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("act_requests.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), unique=True
    )
    position: Mapped[int] = mapped_column(Integer())
    text_state: Mapped[FileTextState] = mapped_column(
        file_text_state_column_type, default=FileTextState.PENDING
    )

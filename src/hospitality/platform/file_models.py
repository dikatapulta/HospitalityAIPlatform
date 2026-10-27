"""Метаданные загруженных файлов — таблица `uploaded_files` (spec 0047 §7, миграция 0036).

Байтов здесь нет (FOUNDATION §9): они — шифротекстом в хранилище за портом
`FileStoragePort`, по `storage_key`. Читать и писать таблицу — только через
`platform/files.py`: ключ, шифрование и файл в хранилище живут одной транзакцией с
этой строкой. Отдельный файл, а не `models.py`: тот уже за пределом R-3.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class FilePurpose(enum.StrEnum):
    """Назначение файла — задаёт его правила (spec 0047 §2, `PURPOSE_POLICIES`).

    VARCHAR без CHECK (довод `StaffRole`): новое назначение — правка кода, а не ALTER.
    """

    MENU_PHOTO = "menu_photo"
    REQUEST_PHOTO = "request_photo"
    GUEST_DOCUMENT = "guest_document"


class UploadedFile(Base):
    """Тенантная таблица по канону `TenantIsolationCanary` (P-4, ADR-003)."""

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    purpose: Mapped[FilePurpose] = mapped_column(
        Enum(
            FilePurpose,
            name="file_purpose",
            native_enum=False,
            length=32,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    # `{tenant_id}/{purpose}/{id}.{ext}` — тенант в ключе: вторая стена к RLS.
    storage_key: Mapped[str] = mapped_column(String(160), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger())
    # SHA-256 хранимого открытого содержимого (после перекодирования), hex.
    sha256: Mapped[str] = mapped_column(String(64))
    # ПД (docs/PII_REGISTRY.md): имя файла может назвать компанию или человека.
    original_name: Mapped[str] = mapped_column(String(255))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Без FK: ядро не ссылается на таблицы модулей (довод `staff_action_log.request_id`).
    guest_identity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


# Единственный обход таблицы — ретеншн: назначения со сроком, старше даты (spec 0047 §5).
Index(
    "ix_uploaded_files_tenant_id_purpose_created_at",
    UploadedFile.tenant_id,
    UploadedFile.purpose,
    UploadedFile.created_at,
)

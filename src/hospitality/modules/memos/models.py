"""ORM-модели модуля memos: служебные записки и объяснительные (spec 0077 §8).

Все шесть таблиц тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0071 (копия канона 0002).

Тексты (заголовок, текст, замечание, комментарий решения), штрихи образца подписи,
хэш содержимого и доказательства подтверждения — шифртекст канона
`platform/field_encryption.py` (колонки `*_ciphertext`, формат — `codec.py`).
Хэш и доказательство шифруются тоже: иначе по копии базы можно проверить догадку
о коротком тексте (ревью PR #938). Сотрудник — FK на `users` без каскада: учётки
гасятся, а не удаляются (ADR-008 §1), и запись переживает увольнение автора.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
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


class MemoKind(enum.StrEnum):
    MEMO = "memo"  # служебная записка, «СЗ-2026/14»
    EXPLANATION = "explanation"  # объяснительная, «ОБ-2026/3» (код — PR 5)


class MemoStatus(enum.StrEnum):
    """Ход записки (§3): `draft` → `signing` → `submitted` → решение; отзыв — до `submitted`."""

    DRAFT = "draft"
    SIGNING = "signing"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETURNED = "returned"
    WITHDRAWN = "withdrawn"


class SignatureRole(enum.StrEnum):
    AUTHOR = "author"  # подтвердил отправку
    SIGNER = "signer"  # подписант
    ADDRESSEE = "addressee"  # подтвердил решение


class SignatureResponse(enum.StrEnum):
    SIGNED = "signed"  # у автора и адресата — подтвердил своё действие
    REMARK = "remark"  # «Есть замечание»


def _string_enum(enum_class: type[enum.StrEnum], name: str) -> Enum:
    # Канон `request_status`: VARCHAR со значениями `.value`, а не тип Postgres.
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


def _tenant_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )


class MemoDemand(Base):
    """Требование письменного объяснения (§6): фото бумажного и получение. Код — PR 5."""

    __tablename__ = "memo_demands"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = _tenant_column()
    demanded_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # Фото подписанного требования — `uploaded_files` (назначение `memo_demand`, PR 5).
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    received_proof_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Memo(Base):
    """Записка или объяснительная — одна редакция и один лист подписей (§3).

    `number` выдаётся отправкой (`memo_counters`) и не меняется; у черновика — NULL.
    `content_hash_ciphertext` — SHA-256 неизменного содержимого на момент отправки.
    `sent_at` — первая отправка: от неё считается срок хранения (§8); `submitted_at` —
    отправка адресату, она фиксирует лист подписей.
    """

    __tablename__ = "memos"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_memos_tenant_id_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = _tenant_column()
    kind: Mapped[MemoKind] = mapped_column(_string_enum(MemoKind, "memo_kind"))
    number: Mapped[str | None] = mapped_column(String(24))
    template_key: Mapped[str | None] = mapped_column(String(40))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    addressee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title_ciphertext: Mapped[bytes] = mapped_column(LargeBinary())
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary())
    content_hash_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    status: Mapped[MemoStatus] = mapped_column(_string_enum(MemoStatus, "memo_status"))
    based_on_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memos.id", ondelete="SET NULL")
    )
    demand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memo_demands.id", ondelete="SET NULL")
    )
    decision_comment_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MemoSignatureSample(Base):
    """Образец подписи штрихами (§4); новая версия — новая строка, прежняя гасится.

    Действующий образец у сотрудника в отеле один — частичный уникальный индекс."""

    __tablename__ = "memo_signature_samples"
    __table_args__ = (
        Index(
            "uq_memo_signature_samples_current",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = _tenant_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    strokes_ciphertext: Mapped[bytes] = mapped_column(LargeBinary())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MemoSignature(Base):
    """Строка листа подписей (§8): кто, в какой роли, что ответил и чем подтвердил.

    Подписанты черновика — строки без ответа; правка черновика их заменяет. Строки
    автора и адресата появляются их подтверждённым действием. `response` NULL после
    `submitted_at` — «не ответил к моменту отправки»."""

    __tablename__ = "memo_signatures"
    __table_args__ = (
        UniqueConstraint(
            "memo_id", "user_id", "role", name="uq_memo_signatures_memo_id_user_id_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = _tenant_column()
    memo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memos.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[SignatureRole] = mapped_column(_string_enum(SignatureRole, "memo_signature_role"))
    response: Mapped[SignatureResponse | None] = mapped_column(
        _string_enum(SignatureResponse, "memo_signature_response")
    )
    remark_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    # `passkey` | `password` — `staff_confirm.ConfirmationMethod`.
    method: Mapped[str | None] = mapped_column(String(16))
    proof_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary())
    sample_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memo_signature_samples.id"), index=True
    )
    signed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class MemoView(Base):
    """Журнал просмотров (§5): кто и когда открывал отправленную запись."""

    __tablename__ = "memo_views"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = _tenant_column()
    memo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memos.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    viewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class MemoCounter(Base):
    """Последний выданный номер внутри отеля, вида и года (§3.2)."""

    __tablename__ = "memo_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True, default=current_tenant_id
    )
    kind: Mapped[MemoKind] = mapped_column(_string_enum(MemoKind, "memo_kind"), primary_key=True)
    year: Mapped[int] = mapped_column(Integer(), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer())

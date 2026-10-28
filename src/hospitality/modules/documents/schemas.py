"""Pydantic-схемы границ модуля documents (spec 0049 §5, R-6).

Сервис принимает `*Submit`/`*Update` и возвращает `*Read`/`ActRequestRow` —
ORM-объекты и шифртексты наружу модуля не выходят. `tenant_id` в схемах нет:
тенанта задаёт контекст (P-4).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.documents.models import ExtractionState, FileTextState
from hospitality.platform.staff_actions import StaffActor

# Кто правит и подтверждает реквизиты из кабинета — тип ядра (spec 0041 §2.3).
ActingUser = StaffActor

MAX_FILES = 5
MAX_REQUESTS_PER_STAY = 5
FIELD_MAX_LENGTH = 300


class RequisiteField(enum.StrEnum):
    """Поля таблицы реквизитов (spec 0049 §4)."""

    COMPANY_NAME = "company_name"
    BIN_IIN = "bin_iin"
    IIK = "iik"
    BIK = "bik"
    BANK_NAME = "bank_name"
    ADDRESS = "address"


class FieldWarning(enum.StrEnum):
    """Подсказка у поля (Р-5): предупреждение, а не отказ."""

    CHECKSUM = "checksum"  # «Контрольная цифра не сходится — проверьте»
    AMBIGUOUS = "ambiguous"  # «Несколько вариантов — выберите сами»


class RequisitesStatus(enum.StrEnum):
    """Отметка строки: «Проверить», пока бухгалтер не нажал «Проверено» (Р-5)."""

    TO_CHECK = "to_check"
    CHECKED = "checked"


class Requisites(BaseModel):
    """Значения полей; пусто — не найдено или стёрто."""

    company_name: str = Field(default="", max_length=FIELD_MAX_LENGTH)
    bin_iin: str = Field(default="", max_length=FIELD_MAX_LENGTH)
    iik: str = Field(default="", max_length=FIELD_MAX_LENGTH)
    bik: str = Field(default="", max_length=FIELD_MAX_LENGTH)
    bank_name: str = Field(default="", max_length=FIELD_MAX_LENGTH)
    address: str = Field(default="", max_length=FIELD_MAX_LENGTH)


class RequisitesUpdate(BaseModel):
    """Правка бухгалтера: None — поле не трогается, "" — стереть (формат — §4)."""

    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)
    bin_iin: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)
    iik: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)
    bik: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)
    bank_name: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)
    address: str | None = Field(default=None, max_length=FIELD_MAX_LENGTH)


class ActRequestSubmit(BaseModel):
    """Форма гостя (Р-8): Stay и личность — из сессии канала, не со слов гостя."""

    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    # Ключ повтора отправки: тот же ключ — тот же запрос (P-8).
    client_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    # Язык браузера после `normalize_language` канала (Р-9).
    language: Literal["en", "ru", "kk"]
    bill_to: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=2000)


class UploadInput(BaseModel):
    """Файл формы как его прислал браузер: имя и байты (проверяет узел файлов)."""

    filename: str
    data: bytes


class ActRequestRead(BaseModel):
    """Ответ форме гостя: что принято (без реквизитов — их гость и так знает)."""

    id: uuid.UUID
    request_id: uuid.UUID
    room_number: str
    check_in_at: datetime
    check_out_at: datetime
    guest_language: str
    files_count: int
    created_at: datetime


class ActRequestFileRead(BaseModel):
    file_id: uuid.UUID
    position: int
    original_name: str
    content_type: str
    text_state: FileTextState


class ActRequestRow(BaseModel):
    """Строка страницы «Документы для АВР» — с расшифрованными полями (PR 3)."""

    id: uuid.UUID
    request_id: uuid.UUID
    room_number: str
    check_in_at: datetime
    check_out_at: datetime
    guest_language: str
    bill_to: str
    note: str
    requisites: Requisites
    warnings: dict[RequisiteField, FieldWarning]
    edited_fields: list[RequisiteField]
    extraction_state: ExtractionState
    status: RequisitesStatus
    checked_at: datetime | None
    checked_by_display_name: str | None
    anonymized: bool
    files: list[ActRequestFileRead]
    created_at: datetime

"""Pydantic-схемы границ модуля documents (spec 0049 §5, spec 0086 §3, R-6).

Сервис принимает `*Submit`/`*Update` и возвращает `*Read`/`ActRequestRow` —
ORM-объекты и шифртексты наружу модуля не выходят. `tenant_id` в схемах нет:
тенанта задаёт контекст (P-4).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hospitality.modules.documents.models import ExtractionState, FileTextState
from hospitality.platform.staff_actions import StaffActor

# Кто правит и подтверждает реквизиты из кабинета — тип ядра (spec 0041 §2.3).
ActingUser = StaffActor

MAX_FILES = 5
MAX_REQUESTS_PER_STAY = 5
FIELD_MAX_LENGTH = 300
# Черновик АВР (spec 0086 Р-3): строк в акте и потолок «Итого» — NUMERIC(14, 2).
MAX_ACT_LINES: Final = 20
MAX_ACT_TOTAL: Final = Decimal("999999999999.99")
_TIYN: Final = Decimal("0.01")


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
    """Что принято (без реквизитов — их гость и так знает).

    `stay_id` и `guest_identity_id` — адрес сообщений гостю «Готово» и «Вернули»
    (подписчик канала, Р-2; ревью PR #510, Н-29).
    """

    id: uuid.UUID
    request_id: uuid.UUID
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
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


def line_cost(quantity: Decimal, price: Decimal) -> Decimal:
    """Стоимость строки акта: количество × цена, до тиына half-up (spec 0086 Р-3)."""
    return (quantity * price).quantize(_TIYN, rounding=ROUND_HALF_UP)


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


class WorkCompletionActLine(BaseModel):
    """Строка акта, которую вписал бухгалтер: услуга, дата оказания, единица, кол-во, цена."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    service: str = Field(min_length=1, max_length=200)
    # Графа 3 формы — только когда даты разные: текст («10.11–12.11.2026»), не дата.
    service_date: str = Field(default="", max_length=40)
    unit: str = Field(min_length=1, max_length=20)
    quantity: Decimal = Field(gt=0, le=99_999, decimal_places=3)
    price: Decimal = Field(ge=0, le=Decimal("9999999999.99"), decimal_places=2)

    @field_validator("service", "service_date", "unit")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if _has_control_chars(value):
            raise ValueError("control characters are not allowed")
        return value


class WorkCompletionActCreate(BaseModel):
    """Форма «Сформировать АВР» (spec 0086 Р-3): из PMS ничего не берётся."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Нумерация живёт в 1С отеля — номер вписывает бухгалтер.
    number: str = Field(min_length=1, max_length=30)
    composed_on: date
    contract: str = Field(default="", max_length=100)
    lines: list[WorkCompletionActLine] = Field(min_length=1, max_length=MAX_ACT_LINES)

    @field_validator("number", "contract")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if _has_control_chars(value):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def _total_fits(self) -> WorkCompletionActCreate:
        total = sum((line_cost(line.quantity, line.price) for line in self.lines), Decimal(0))
        if total > MAX_ACT_TOTAL:
            raise ValueError(f"act total must not exceed {MAX_ACT_TOTAL}")
        return self


class WorkCompletionActRead(BaseModel):
    """Строка списка актов под запросом: без реквизитов и строк (они — в файле)."""

    id: uuid.UUID
    act_request_id: uuid.UUID
    number: str
    composed_on: date
    total: Decimal
    created_by_display_name: str | None
    created_at: datetime
    anonymized: bool

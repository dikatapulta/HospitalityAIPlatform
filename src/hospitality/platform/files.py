"""Файлы тенанта — единственная дверь модулей к хранилищу (spec 0047, ADR-022).

CANONICAL: приём файла от гостя или персонала — только через эти функции.

Модуль, принимающий файл (room service — фото блюд, documents — файлы гостя,
заявки — фото), зовёт только эти функции и хранит у себя id строки
`uploaded_files`. Порт хранилища, проверку содержимого и шифрование он не видит.
Маршрут читает тело запроса только `upload_form.read_upload_form`: потолок
тела до разбора, файлы — в памяти, а не открытым текстом во временном файле.

Путь загрузки: проверка содержимого в дочернем процессе (`file_sandbox`, §11) →
шифрование AES-256-GCM ключом `FILE_ENCRYPTION_KEY` → `put` в хранилище →
строка метаданных. Порядок «файл, потом строка»: упади вставка строки — файл
удаляется тут же; упади процесс между ними — останется бесхозный шифртекст без
строки, но не строка без файла.

Мультитенантность (P-4): строка — тенантная таблица с RLS, чтение и удаление
идут по id внутри контекста тенанта — чужой id даёт ERR-FILES-004, как
несуществующий. Тенант ещё и в ключе объекта, а ключ — AAD шифрования.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ForeignKey, Index, Integer, String, Uuid, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.platform.file_content import (
    FileDecryptError,
    FilePurpose,
    FileRejectedError,
    RejectReason,
    decrypt_object,
    encrypt_object,
)
from hospitality.platform.file_sandbox import SandboxFailedError, prepare_upload_isolated
from hospitality.shared.config import (
    InvalidFileEncryptionKeyError,
    file_encryption_key_bytes,
    get_settings,
)
from hospitality.shared.db import Base, UTCDateTime, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_file_operation
from hospitality.shared.ports.file_storage import (
    StoredObjectNotFoundError,
    build_storage_key,
    get_file_storage,
)
from hospitality.shared.tenancy import current_tenant_id

__all__ = [
    "FilePurpose",
    "StoredFile",
    "UploadedFile",
    "UploadedFileRead",
    "attachment_disposition",
    "delete_expired_files",
    "delete_file",
    "file_response",
    "read_file",
    "store_upload",
]

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8). Тексты видят гость и
# персонал — экраны загрузки (#418, #493) показывают их как есть.
ERR_FILE_TOO_LARGE = "ERR-FILES-001"
ERR_FILE_TYPE = "ERR-FILES-002"
ERR_FILE_MACROS = "ERR-FILES-003"
ERR_FILE_NOT_FOUND = "ERR-FILES-004"
ERR_FILE_KEY = "ERR-FILES-005"
ERR_FILE_STORAGE = "ERR-FILES-006"

_PHOTO_ONLY_TEXT = "Такой файл не подходит. Нужна фотография: JPG, PNG или WebP."
_DOCUMENT_TEXT = "Такой файл не подходит. Подойдёт фото, PDF или документ Word (.docx)."
_ORIGINAL_NAME_MAX = 255


class UploadedFile(Base):
    """Метаданные файла в хранилище; сам файл — по `storage_key` (spec 0047 §3).

    Тенантная таблица по канону `TenantIsolationCanary` (P-4). `guest_identity_id`
    без FK: ядро не ссылается на таблицы модулей (как `staff_action_log.stay_id`).
    Удаление жёсткое — файл и строка; след остаётся в логе `file_deleted`.
    """

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    purpose: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(200), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer())
    sha256: Mapped[str] = mapped_column(String(64))
    # Имя файла у загрузившего — PII (docs/PII_REGISTRY.md): в «Реквизиты
    # ТОО Ромашка.pdf» название компании, в «IMG_Иванов.jpg» — фамилия.
    original_name: Mapped[str] = mapped_column(String(_ORIGINAL_NAME_MAX))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    guest_identity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


# Единственная выборка по времени — ретеншн: назначение + возраст.
Index(
    "ix_uploaded_files_tenant_purpose_created",
    UploadedFile.tenant_id,
    UploadedFile.purpose,
    UploadedFile.created_at,
)


class UploadedFileRead(BaseModel):
    """Публичное представление строки — то, что модуль хранит и показывает."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: FilePurpose
    content_type: str
    size_bytes: int
    original_name: str
    created_at: datetime


class StoredFile(BaseModel):
    """Файл целиком для выдачи: метаданные и расшифрованные байты."""

    meta: UploadedFileRead
    data: bytes


async def store_upload(
    purpose: FilePurpose,
    data: bytes,
    original_name: str,
    *,
    uploaded_by_user_id: uuid.UUID | None = None,
    guest_identity_id: uuid.UUID | None = None,
) -> UploadedFileRead:
    """Принять файл в контексте текущего тенанта; негодный — ERR-FILES-001…003."""
    tenant_id = current_tenant_id()
    key = _encryption_key("store")
    try:
        prepared = await prepare_upload_isolated(purpose, data)
    except SandboxFailedError as exc:
        record_file_operation("store", "error", "sandbox")
        logger.warning(
            "file_sandbox_failed",
            error_code=ERR_FILE_STORAGE,
            purpose=purpose.value,
            detail=str(exc),
        )
        raise AppError(
            code=ERR_FILE_STORAGE,
            message="Не удалось сохранить файл. Попробуйте ещё раз чуть позже.",
            status_code=503,
        ) from exc
    except FileRejectedError as exc:
        record_file_operation("store", "rejected", exc.reason)
        logger.info("file_rejected", purpose=purpose.value, reason=exc.reason, size_bytes=len(data))
        raise _rejection_error(purpose, exc.reason) from exc

    file_id = uuid.uuid4()
    storage_key = build_storage_key(tenant_id, purpose.value, file_id, prepared.extension)
    blob = encrypt_object(prepared.data, key, storage_key)
    storage = get_file_storage()
    try:
        await storage.put(storage_key, blob, prepared.content_type)
    except OSError as exc:
        raise _storage_failure("store", file_id) from exc
    try:
        async with session_scope() as session:
            row = UploadedFile(
                id=file_id,
                purpose=purpose.value,
                storage_key=storage_key,
                content_type=prepared.content_type,
                size_bytes=len(prepared.data),
                sha256=prepared.sha256,
                original_name=_clean_original_name(original_name, prepared.extension),
                uploaded_by_user_id=uploaded_by_user_id,
                guest_identity_id=guest_identity_id,
            )
            session.add(row)
            await session.flush()
            result = UploadedFileRead.model_validate(row)
    except Exception:
        await storage.delete(storage_key)
        raise
    record_file_operation("store", "ok")
    logger.info(
        "file_stored",
        file_id=str(file_id),
        purpose=purpose.value,
        content_type=prepared.content_type,
        size_bytes=len(prepared.data),
    )
    return result


async def read_file(file_id: uuid.UUID) -> StoredFile:
    """Файл тенанта по id; нет (или чужой) — ERR-FILES-004."""
    key = _encryption_key("read")
    row = await _load_row(file_id, "read")
    try:
        blob = await get_file_storage().get(row.storage_key)
    except (StoredObjectNotFoundError, OSError) as exc:
        # Строка есть, а файла нет — хранилище потеряло данные (том не
        # смонтирован, восстановлена БД без тома): это авария, не 404.
        raise _storage_failure("read", file_id) from exc
    try:
        data = decrypt_object(blob, key, row.storage_key)
    except FileDecryptError as exc:
        record_file_operation("read", "error", "decrypt")
        logger.warning("file_decrypt_failed", error_code=ERR_FILE_KEY, file_id=str(file_id))
        raise AppError(
            code=ERR_FILE_KEY, message="Файлы сейчас недоступны.", status_code=503
        ) from exc
    record_file_operation("read", "ok")
    return StoredFile(meta=UploadedFileRead.model_validate(row), data=data)


async def delete_file(file_id: uuid.UUID) -> None:
    """Удалить файл и строку; нет (или чужой) — ERR-FILES-004."""
    row = await _load_row(file_id, "delete")
    try:
        await get_file_storage().delete(row.storage_key)
    except OSError as exc:
        raise _storage_failure("delete", file_id) from exc
    async with session_scope() as session:
        await session.execute(delete(UploadedFile).where(UploadedFile.id == file_id))
    record_file_operation("delete", "ok")
    logger.info("file_deleted", file_id=str(file_id), purpose=row.purpose)


async def delete_expired_files(purpose: FilePurpose, *, created_before: datetime) -> int:
    """Ретеншн одного назначения у текущего тенанта: файл, затем строка; вернуть число.

    Зовёт джоба ретеншна (`channels/common/retention.py`, spec 0032). Сначала
    файл, потом строка: упади удаление файла — строка доживёт до следующего
    прогона и повтор его доделает (P-8); обратный порядок оставил бы файл гостя
    без строки, то есть навсегда.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(UploadedFile.id, UploadedFile.storage_key).where(
                    UploadedFile.purpose == purpose.value,
                    UploadedFile.created_at < created_before,
                )
            )
        ).all()
    if not rows:
        return 0
    storage = get_file_storage()
    for _, storage_key in rows:
        await storage.delete(storage_key)
    async with session_scope() as session:
        deleted = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(UploadedFile).where(UploadedFile.id.in_([row_id for row_id, _ in rows]))
            ),
        ).rowcount
    return deleted or 0


def file_response(stored: StoredFile) -> Response:
    """HTTP-ответ с файлом по правилам безопасной выдачи (spec 0047 §5).

    PDF и DOCX — только вложением: браузер их не открывает, скрипт внутри PDF
    не исполнится на нашем домене. `nosniff` и `CSP: sandbox` — на всё:
    браузер не угадывает тип и не даёт файлу прав страницы. Файлы гостя не
    кэшируются нигде.
    """
    meta = stored.meta
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": (
            "no-store" if meta.purpose is FilePurpose.GUEST_DOCUMENT else "private, max-age=3600"
        ),
    }
    if not meta.content_type.startswith("image/"):
        headers["Content-Disposition"] = attachment_disposition(meta.original_name)
    return Response(content=stored.data, media_type=meta.content_type, headers=headers)


async def _load_row(file_id: uuid.UUID, operation: str) -> UploadedFile:
    async with session_scope() as session:
        row = await session.get(UploadedFile, file_id)
    if row is None:
        record_file_operation(operation, "rejected", "not_found")
        raise AppError(code=ERR_FILE_NOT_FOUND, message="Файл не найден.", status_code=404)
    return row


# Сбои ниже — на пути запроса гостя или сотрудника: WARNING, не ERROR. ERROR-лог
# становится событием Sentry с телом запроса (файлом), пока не закрыт #433.


def _encryption_key(operation: str) -> bytes:
    try:
        key = file_encryption_key_bytes(get_settings())
    except InvalidFileEncryptionKeyError:
        key = None
    if key is None:
        record_file_operation(operation, "error", "no_key")
        logger.warning("file_encryption_key_missing", error_code=ERR_FILE_KEY)
        raise AppError(code=ERR_FILE_KEY, message="Файлы сейчас недоступны.", status_code=503)
    return key


def _storage_failure(operation: str, file_id: uuid.UUID) -> AppError:
    record_file_operation(operation, "error", "storage")
    logger.warning(
        "file_storage_failed",
        error_code=ERR_FILE_STORAGE,
        operation=operation,
        file_id=str(file_id),
        exc_info=True,
    )
    return AppError(
        code=ERR_FILE_STORAGE,
        message="Не удалось сохранить файл. Попробуйте ещё раз чуть позже."
        if operation == "store"
        else "Файлы сейчас недоступны.",
        status_code=503,
    )


def _rejection_error(purpose: FilePurpose, reason: RejectReason) -> AppError:
    if reason is RejectReason.TOO_LARGE:
        return AppError(
            code=ERR_FILE_TOO_LARGE,
            message="Файл слишком большой. Фото — до 5 МБ, документ — до 10 МБ.",
            status_code=413,
        )
    if reason is RejectReason.MACROS:
        return AppError(
            code=ERR_FILE_MACROS,
            message=(
                "В документе есть макросы — такой файл принять нельзя. "
                "Сохраните его как обычный документ Word (.docx) или PDF."
            ),
            status_code=415,
        )
    documents_allowed = purpose is FilePurpose.GUEST_DOCUMENT
    return AppError(
        code=ERR_FILE_TYPE,
        message=_DOCUMENT_TEXT if documents_allowed else _PHOTO_ONLY_TEXT,
        status_code=415,
    )


def _clean_original_name(name: str, extension: str) -> str:
    """Имя для показа и скачивания: без пути и управляющих символов, с нашим расширением.

    Расширение — от проверенного содержимого: «фото.jpg», ставшее WebP,
    скачивается как «фото.webp», а «счёт.pdf.exe» не бывает по построению.
    """
    base = PureWindowsPath(PurePosixPath(name).name).name
    stem = "".join(ch for ch in base if ch.isprintable()).strip().rsplit(".", 1)[0].strip()
    stem = stem or "file"
    return f"{stem[: _ORIGINAL_NAME_MAX - len(extension) - 1]}.{extension}"


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition: attachment` с именем файла — и для выгрузок кабинета."""
    # RFC 6266: ASCII-запасное имя для старых клиентов и UTF-8 — для остальных.
    fallback = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"

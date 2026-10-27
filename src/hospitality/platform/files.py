"""Хранилище файлов — единственная дверь для модулей (spec 0047, ADR-022, issue #491).

Модуль, принимающий файл (фото блюда #418, реквизиты гостя #493, фото к заявке #82), зовёт
`store_upload` и хранит у себя только `UploadedFileRead.id`; отдаёт файл —
`read_file` + `file_response`. Тип по содержимому, разбор в дочернем процессе, шифрование,
ключ хранения с тенантом и строка `uploaded_files` — здесь, одним путём для всех (P-12).

Адаптер хранилища подключает composition root (`configure_file_storage` в `app.py` и
`worker.py`): ядро видит только порт (P-3). Все функции работают в контексте тенанта.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from collections.abc import Awaitable
from datetime import datetime
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select

from hospitality.platform.file_crypto import (
    FileDecryptionError,
    decrypt_file,
    encrypt_file,
    load_file_encryption_key,
)
from hospitality.platform.file_inspection import (
    FileInspectionError,
    FileKind,
    sanitize,
    sniff_kind,
)
from hospitality.platform.file_models import FilePurpose, UploadedFile
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_file_storage_error, record_file_upload
from hospitality.shared.ports.file_storage import (
    FileStorageError,
    FileStoragePort,
    StoredObjectNotFoundError,
)
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Каталог ошибок — docs/runbooks/errors.md; таблица — spec 0047 §8.
ERR_FILE_TOO_LARGE = "ERR-FILES-001"
ERR_FILE_TYPE_NOT_ALLOWED = "ERR-FILES-002"
ERR_FILE_CONTENT_REJECTED = "ERR-FILES-003"
ERR_FILE_HAS_MACROS = "ERR-FILES-004"
ERR_FILE_NOT_FOUND = "ERR-FILES-005"
ERR_FILE_STORAGE_NOT_CONFIGURED = "ERR-FILES-006"
ERR_FILE_STORAGE_FAILED = "ERR-FILES-007"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MAX_NAME_CHARS = 255


@dataclasses.dataclass(frozen=True)
class PurposePolicy:
    kinds: frozenset[FileKind]
    image_max_side: int
    # Срок spec 0032 (90 дней от загрузки) — или живёт, пока его не удалит владелец.
    expires_with_guest_texts: bool
    browser_cache_seconds: int


PURPOSE_POLICIES: dict[FilePurpose, PurposePolicy] = {
    FilePurpose.MENU_PHOTO: PurposePolicy(frozenset({FileKind.IMAGE}), 960, False, 86400),
    FilePurpose.REQUEST_PHOTO: PurposePolicy(frozenset({FileKind.IMAGE}), 1600, True, 0),
    FilePurpose.GUEST_DOCUMENT: PurposePolicy(
        frozenset({FileKind.IMAGE, FileKind.PDF, FileKind.DOCX}), 2048, True, 0
    ),
}

_STORED_FORMAT: dict[FileKind, tuple[str, str]] = {
    FileKind.IMAGE: ("image/webp", "webp"),
    FileKind.PDF: ("application/pdf", "pdf"),
    FileKind.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
}
_KIND_SUFFIXES: dict[FileKind, frozenset[str]] = {
    FileKind.IMAGE: frozenset({"jpg", "jpeg", "png", "webp"}),
    FileKind.PDF: frozenset({"pdf"}),
    FileKind.DOCX: frozenset({"docx"}),
}


class UploadedFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: FilePurpose
    content_type: str
    size_bytes: int
    sha256: str
    original_name: str
    uploaded_by_user_id: uuid.UUID | None
    guest_identity_id: uuid.UUID | None
    created_at: datetime


@dataclasses.dataclass(frozen=True)
class StoredFile:
    file: UploadedFileRead
    content: bytes


_storage: FileStoragePort | None = None


def configure_file_storage(storage: FileStoragePort | None) -> None:
    """Подключить адаптер — только из composition root (и тестов); `None` — отключить."""
    global _storage
    _storage = storage


async def check_file_storage_health() -> bool:
    """Проверка `/health/ready` (spec 0047 §6): адаптер подключён и пишет."""
    return _storage is not None and await _storage.check_health()


async def store_upload(
    purpose: FilePurpose,
    data: bytes,
    original_name: str,
    *,
    uploaded_by_user_id: uuid.UUID | None = None,
    guest_identity_id: uuid.UUID | None = None,
) -> UploadedFileRead:
    """Проверить, перекодировать, зашифровать и сохранить файл (spec 0047 §3)."""
    storage, encryption_key = _require_storage(), _require_encryption_key()
    policy = PURPOSE_POLICIES[purpose]
    if len(data) > MAX_UPLOAD_BYTES:
        raise _rejected(purpose, "rejected_size", ERR_FILE_TOO_LARGE, len(data))
    kind = sniff_kind(data)
    if kind is None or kind not in policy.kinds:
        raise _rejected(purpose, "rejected_type", ERR_FILE_TYPE_NOT_ALLOWED, len(data))
    try:
        stored = await sanitize(kind, data, policy.image_max_side)
    except FileInspectionError as rejection:
        code = {"macros": ERR_FILE_HAS_MACROS, "not_docx": ERR_FILE_TYPE_NOT_ALLOWED}.get(
            rejection.reason, ERR_FILE_CONTENT_REJECTED
        )
        outcome = "rejected_type" if code == ERR_FILE_TYPE_NOT_ALLOWED else "rejected_content"
        raise _rejected(purpose, outcome, code, len(data), rejection.reason) from None

    content_type, extension = _STORED_FORMAT[kind]
    file_id = uuid.uuid4()
    key = f"{current_tenant_id()}/{purpose.value}/{file_id}.{extension}"
    row = UploadedFile(
        id=file_id,
        purpose=purpose,
        storage_key=key,
        content_type=content_type,
        size_bytes=len(stored),
        sha256=hashlib.sha256(stored).hexdigest(),
        original_name=_clean_name(original_name, kind),
        uploaded_by_user_id=uploaded_by_user_id,
        guest_identity_id=guest_identity_id,
    )
    blob = encrypt_file(encryption_key, stored, key.encode())
    try:
        async with session_scope() as session:
            session.add(row)
            await session.flush()
            await _storage_call("put", storage.put(key, blob))
            result = UploadedFileRead.model_validate(row)
    except Exception:
        # Строка не записалась — файл без строки никто не найдёт и не удалит ретеншном.
        await _delete_quietly(storage, key)
        raise
    record_file_upload(purpose.value, "stored")
    logger.info(
        "file_stored",
        file_id=str(file_id),
        purpose=purpose.value,
        content_type=content_type,
        size_bytes=len(stored),
        received_bytes=len(data),
    )
    return result


async def read_file(file_id: uuid.UUID) -> StoredFile:
    """Метаданные и расшифрованное содержимое; чужой или несуществующий id — ERR-FILES-005."""
    storage, encryption_key = _require_storage(), _require_encryption_key()
    async with session_scope() as session:
        row = await session.get(UploadedFile, file_id)
        if row is None:
            raise _not_found(file_id)
        metadata = UploadedFileRead.model_validate(row)
        key = row.storage_key
    if not key.startswith(f"{current_tenant_id()}/"):
        # RLS уже отдал строку этому тенанту — ключ чужого тенанта значит порчу данных.
        raise _storage_failed("tenant_mismatch", file_id)
    blob = await _storage_call("get", storage.get(key), file_id)
    try:
        content = decrypt_file(encryption_key, blob, key.encode())
    except FileDecryptionError:
        raise _storage_failed("decrypt", file_id) from None
    return StoredFile(file=metadata, content=content)


async def delete_file(file_id: uuid.UUID) -> None:
    """Удалить файл и строку (spec 0047 §5) — например, снятое фото блюда."""
    storage = _require_storage()
    async with session_scope() as session:
        row = await session.get(UploadedFile, file_id)
        if row is None:
            raise _not_found(file_id)
        await _storage_call("delete", storage.delete(row.storage_key), file_id)
        await session.delete(row)
    logger.info("file_deleted", file_id=str(file_id), purpose=row.purpose.value)


async def delete_expired_files(*, created_before: datetime) -> int:
    """Ретеншн (spec 0032, spec 0047 §5): файлы назначений со сроком, загруженные до даты.

    Файлы — раньше строк: упавшее посередине удаление откатывает строки, и следующий прогон
    повторит его (удаление в хранилище идемпотентно). Нечего удалять — к хранилищу не идём.
    """
    expiring = [
        purpose for purpose, policy in PURPOSE_POLICIES.items() if policy.expires_with_guest_texts
    ]
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(UploadedFile.id, UploadedFile.storage_key).where(
                    UploadedFile.purpose.in_(expiring),
                    UploadedFile.created_at < created_before,
                )
            )
        ).all()
        if not rows:
            return 0
        storage = _require_storage()
        for row in rows:
            await _storage_call("delete", storage.delete(row.storage_key), row.id)
        await session.execute(
            delete(UploadedFile).where(UploadedFile.id.in_([row.id for row in rows]))
        )
    return len(rows)


def file_response(stored: StoredFile) -> Response:
    """Единственный способ отдать файл в HTTP (spec 0047 §4): заголовки безопасности всегда."""
    metadata = stored.file
    cache_seconds = PURPOSE_POLICIES[metadata.purpose].browser_cache_seconds
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": f"private, max-age={cache_seconds}" if cache_seconds else "no-store",
    }
    if metadata.content_type == "image/webp":
        headers["Content-Disposition"] = "inline"
    else:
        # PDF и DOCX в браузере не открываются: только скачивание (ADR-022 п. 4).
        fallback = metadata.original_name.encode("ascii", "replace").decode().replace('"', "_")
        headers["Content-Disposition"] = (
            f'attachment; filename="{fallback.replace("?", "_")}"; '
            f"filename*=UTF-8''{quote(metadata.original_name)}"
        )
    return Response(content=stored.content, media_type=metadata.content_type, headers=headers)


def _clean_name(original_name: str, kind: FileKind) -> str:
    """Последний сегмент пути без управляющих символов; расширение — по настоящему типу.

    Своё расширение типа заменяется («реквизиты.png» → «реквизиты.webp»), чужое остаётся
    частью имени: «счёт.exe» с PDF внутри скачается как «счёт.exe.pdf», а не как программа.
    """
    name = original_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).strip() or "file"
    stem, dot, suffix = name.rpartition(".")
    if dot and stem and suffix.lower() in _KIND_SUFFIXES[kind]:
        name = stem
    extension = _STORED_FORMAT[kind][1]
    return f"{name[: _MAX_NAME_CHARS - len(extension) - 1]}.{extension}"


def _require_storage() -> FileStoragePort:
    if _storage is None:
        # WARNING, а не ERROR, — довод `_storage_failed` (#433).
        logger.warning("file_storage_not_configured", error_code=ERR_FILE_STORAGE_NOT_CONFIGURED)
        raise _not_configured()
    return _storage


def _require_encryption_key() -> bytes:
    # Негодный ключ сюда не доходит: его отвергает preflight на старте процесса.
    key = load_file_encryption_key(get_settings().file_encryption_key)
    if key is None:
        logger.warning("file_encryption_key_missing", error_code=ERR_FILE_STORAGE_NOT_CONFIGURED)
        raise _not_configured()
    return key


async def _storage_call[T](
    operation: str, call: Awaitable[T], file_id: uuid.UUID | None = None
) -> T:
    try:
        return await call
    except StoredObjectNotFoundError:
        raise _storage_failed("missing", file_id) from None
    except FileStorageError as error:
        raise _storage_failed(operation, file_id, str(error)) from None


async def _delete_quietly(storage: FileStoragePort, key: str) -> None:
    try:
        await storage.delete(key)
    except FileStorageError as error:
        logger.warning(
            "file_orphan_left",
            error_code=ERR_FILE_STORAGE_FAILED,
            storage_key=key,
            error=str(error),
        )


def _rejected(
    purpose: FilePurpose, outcome: str, code: str, size: int, reason: str = ""
) -> AppError:
    record_file_upload(purpose.value, outcome)
    logger.info(
        "file_upload_rejected",
        error_code=code,
        purpose=purpose.value,
        size_bytes=size,
        reason=reason or outcome,
    )
    images_only = PURPOSE_POLICIES[purpose].kinds == {FileKind.IMAGE}
    messages = {
        ERR_FILE_TOO_LARGE: f"Файл слишком большой — можно до {MAX_UPLOAD_BYTES // 2**20} МБ.",
        ERR_FILE_TYPE_NOT_ALLOWED: (
            "Нужна фотография: JPG, PNG или WebP."
            if images_only
            else "Такой файл не подходит. Пришлите фото (JPG, PNG), PDF или Word (DOCX)."
        ),
        ERR_FILE_CONTENT_REJECTED: (
            "Не получилось открыть файл. Пришлите другой или сфотографируйте заново."
        ),
        ERR_FILE_HAS_MACROS: (
            "Документ с макросами не принимаем — сохраните его как обычный DOCX или PDF."
        ),
    }
    statuses = {ERR_FILE_TOO_LARGE: 413, ERR_FILE_TYPE_NOT_ALLOWED: 415}
    return AppError(code=code, message=messages[code], status_code=statuses.get(code, 422))


def _not_found(file_id: uuid.UUID) -> AppError:
    logger.info("file_not_found", error_code=ERR_FILE_NOT_FOUND, file_id=str(file_id))
    return AppError(
        code=ERR_FILE_NOT_FOUND,
        message="Файл не найден — возможно, его уже удалили.",
        status_code=404,
    )


def _not_configured() -> AppError:
    return AppError(
        code=ERR_FILE_STORAGE_NOT_CONFIGURED,
        message="Файлы сейчас не принимаются — попробуйте позже.",
        status_code=503,
    )


def _storage_failed(reason: str, file_id: uuid.UUID | None, error: str = "") -> AppError:
    record_file_storage_error(reason)
    # WARNING, а не ERROR, — пока открыт #433: ERROR-лог на пути запроса становится
    # событием Sentry вместе с телом запроса, а тело здесь — файл гостя (ст. 12 ЗПД РК).
    logger.warning(
        "file_storage_failed",
        error_code=ERR_FILE_STORAGE_FAILED,
        reason=reason,
        file_id=str(file_id) if file_id else None,
        error=error or None,
    )
    return AppError(
        code=ERR_FILE_STORAGE_FAILED,
        message="Файл сейчас недоступен — попробуйте позже.",
        status_code=503,
    )

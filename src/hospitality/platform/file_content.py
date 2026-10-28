"""Содержимое загружаемого файла: что принимаем, как проверяем, как шифруем (spec 0047 §4–§5).

Чистые функции без БД и без хранилища. `prepare_upload` исполняется в дочернем
процессе (`platform/file_sandbox.py`, FOUNDATION §11), шифрование — в сервисе
`platform/files.py`.

Тип определяется по байтам, а не по имени и не по `Content-Type` клиента:
`%PDF-` — PDF, zip с `word/document.xml` — DOCX, иначе — попытка декодировать
картинку (JPEG, PNG, WebP). Картинка перекодируется в WebP со стороной не
больше лимита назначения и без метаданных: EXIF с геолокацией не уезжает, а
сама перекодировка и есть проверка — не декодируется, значит не картинка.
PDF и DOCX хранятся как есть: здесь их не разбирают (FOUNDATION §11 — разбор
документов в воркере, это #493), только проверяют сигнатуру и отсутствие
макросов у DOCX.
"""

from __future__ import annotations

import enum
import hashlib
import io
import os
import warnings
import zipfile
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageOps, UnidentifiedImageError


class FilePurpose(enum.StrEnum):
    """Назначение файла: от него зависят допустимые типы, лимиты и срок хранения."""

    MENU_PHOTO = "menu_photo"  # фото блюда или товара (#418)
    GUEST_DOCUMENT = "guest_document"  # реквизиты, доверенность и т.п. от гостя (#493)
    REQUEST_PHOTO = "request_photo"  # фото к заявке (#82)


class FileKind(enum.StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    DOCX = "docx"


IMAGE_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
# Предел декодирования: 40 Мп — фото любого телефона с запасом; больше —
# «бомба» (PNG в килобайты, разворачивающийся в гигабайты памяти).
IMAGE_MAX_PIXELS = 40_000_000
WEBP_QUALITY = 82

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Что ложится в хранилище по виду файла: content type и расширение. Одна таблица
# на проверку и на родителя песочницы — тот не берёт их из ответа ребёнка.
KIND_FORMATS: dict[FileKind, tuple[str, str]] = {
    FileKind.IMAGE: ("image/webp", "webp"),
    FileKind.PDF: ("application/pdf", "pdf"),
    FileKind.DOCX: (DOCX_CONTENT_TYPE, "docx"),
}
_MACRO_CONTENT_TYPE_MARKER = b"macroenabled"
_CONTENT_TYPES_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PurposeRules:
    kinds: frozenset[FileKind]
    image_max_side: int


# Фото блюда и фото к заявке смотрят на экране — 960 px хватает. Фото
# реквизитов читают глазами и моделью (#493): цифры БИН/ИИК на 960 px
# неразборчивы, поэтому сторона больше.
PURPOSE_RULES: dict[FilePurpose, PurposeRules] = {
    FilePurpose.MENU_PHOTO: PurposeRules(frozenset({FileKind.IMAGE}), 960),
    FilePurpose.REQUEST_PHOTO: PurposeRules(frozenset({FileKind.IMAGE}), 960),
    FilePurpose.GUEST_DOCUMENT: PurposeRules(
        frozenset({FileKind.IMAGE, FileKind.PDF, FileKind.DOCX}), 1600
    ),
}

_KIND_MAX_BYTES = {
    FileKind.IMAGE: IMAGE_MAX_BYTES,
    FileKind.PDF: DOCUMENT_MAX_BYTES,
    FileKind.DOCX: DOCUMENT_MAX_BYTES,
}


class RejectReason(enum.StrEnum):
    TOO_LARGE = "too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    MACROS = "macros"


class FileRejectedError(Exception):
    """Файл не принят; `reason` решает код ошибки (ERR-FILES-001…003)."""

    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class PreparedFile:
    """Что ляжет в хранилище (до шифрования) и в строку `uploaded_files`."""

    kind: FileKind
    data: bytes
    content_type: str
    extension: str
    sha256: str


def max_upload_bytes(purpose: FilePurpose) -> int:
    """Потолок сырого файла для назначения — по самому крупному допустимому типу."""
    return max(_KIND_MAX_BYTES[kind] for kind in PURPOSE_RULES[purpose].kinds)


def prepare_upload(purpose: FilePurpose, data: bytes) -> PreparedFile:
    """Проверить и подготовить файл; негодный — `FileRejectedError`.

    Синхронная и тяжёлая (декодирование картинки): сервис зовёт её в потоке.
    """
    rules = PURPOSE_RULES[purpose]
    if len(data) > max_upload_bytes(purpose):
        raise FileRejectedError(RejectReason.TOO_LARGE, f"{len(data)} bytes")
    kind = _detect_kind(data)
    if kind not in rules.kinds:
        raise FileRejectedError(RejectReason.UNSUPPORTED_TYPE, f"{kind} not allowed")
    if len(data) > _KIND_MAX_BYTES[kind]:
        raise FileRejectedError(RejectReason.TOO_LARGE, f"{len(data)} bytes for {kind}")
    if kind is FileKind.DOCX:
        _reject_macros(data)
    prepared = _reencode_image(data, rules.image_max_side) if kind is FileKind.IMAGE else data
    content_type, extension = KIND_FORMATS[kind]
    return PreparedFile(
        kind=kind,
        data=prepared,
        content_type=content_type,
        extension=extension,
        sha256=hashlib.sha256(prepared).hexdigest(),
    )


def _detect_kind(data: bytes) -> FileKind:
    if data.startswith(b"%PDF-"):
        return FileKind.PDF
    if data.startswith(b"PK\x03\x04"):
        if _is_docx(data):
            return FileKind.DOCX
        raise FileRejectedError(RejectReason.UNSUPPORTED_TYPE, "zip that is not a .docx")
    # Остальное обязано оказаться картинкой — это решит декодирование.
    return FileKind.IMAGE


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, ValueError):
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _reject_macros(data: bytes) -> None:
    """`.docm` или DOCX с `vbaProject.bin` внутри — отказ (spec 0047 §4).

    Читается только оглавление zip и `[Content_Types].xml` с потолком размера:
    архив не распаковывается, zip-бомба здесь не взорвётся.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if any(name.lower().endswith("vbaproject.bin") for name in archive.namelist()):
            raise FileRejectedError(RejectReason.MACROS, "vbaProject.bin inside")
        info = archive.getinfo("[Content_Types].xml")
        if info.file_size > _CONTENT_TYPES_MAX_BYTES:
            raise FileRejectedError(RejectReason.UNSUPPORTED_TYPE, "oversized [Content_Types]")
        if _MACRO_CONTENT_TYPE_MARKER in archive.read(info).lower():
            raise FileRejectedError(RejectReason.MACROS, "macro-enabled content type")


def _reencode_image(data: bytes, max_side: int) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as source:
                if source.width * source.height > IMAGE_MAX_PIXELS:
                    raise FileRejectedError(
                        RejectReason.TOO_LARGE, f"{source.width}x{source.height} pixels"
                    )
                # Сначала уменьшение на месте (JPEG — сразу декодированием в
                # уменьшенном масштабе): копии ниже делаются уже с маленькой
                # картинки. Рамка квадратная — поворот её не нарушит.
                source.thumbnail((max_side, max_side))
                # Поворот из EXIF применяется до того, как EXIF выброшен:
                # иначе фото с телефона легло бы набок.
                image = ImageOps.exif_transpose(source)
                has_alpha = image.mode in ("RGBA", "LA", "PA") or (
                    image.mode == "P" and "transparency" in image.info
                )
                image = image.convert("RGBA" if has_alpha else "RGB")
                # Сохраняется пиксельная копия без info: EXIF, XMP, ICC и
                # комментарии остаются в исходнике и в файл не попадают.
                clean = Image.new(image.mode, image.size)
                clean.paste(image)
                out = io.BytesIO()
                clean.save(out, format="WEBP", quality=WEBP_QUALITY)
                return out.getvalue()
    except FileRejectedError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise FileRejectedError(RejectReason.TOO_LARGE, "decompression bomb") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise FileRejectedError(RejectReason.UNSUPPORTED_TYPE, "not a decodable image") from exc


# --- Шифрование на томе (FOUNDATION §11, ADR-022) ----------------------------
# Формат объекта: версия (1 байт) + nonce (12) + шифртекст с тегом GCM. Версия —
# задел на смену ключа без миграции схемы. AAD — ключ объекта в хранилище:
# файл, переложенный под чужой ключ (другой тенант, другое назначение), не
# расшифруется.

_CIPHER_VERSION = b"\x01"
_NONCE_BYTES = 12


class FileDecryptError(Exception):
    """Объект не расшифровался: другой ключ шифрования или объект подменён."""


def encrypt_object(plaintext: bytes, key: bytes, storage_key: str) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, storage_key.encode())
    return _CIPHER_VERSION + nonce + ciphertext


def decrypt_object(blob: bytes, key: bytes, storage_key: str) -> bytes:
    if not blob.startswith(_CIPHER_VERSION) or len(blob) < 1 + _NONCE_BYTES:
        raise FileDecryptError("unknown object format")
    nonce = blob[1 : 1 + _NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[1 + _NONCE_BYTES :], storage_key.encode())
    except InvalidTag as exc:
        raise FileDecryptError("authentication tag mismatch") from exc

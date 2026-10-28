"""Шифрование полей БД приложением (FOUNDATION §11, spec 0049 Р-3).

CANONICAL: особо чувствительное поле строки (реквизиты гостя, потом — ключи
интеграций) хранится шифртекстом через эти две функции, а не открытым текстом.

Ключ и формат — те же, что у файлов на томе (ADR-022, spec 0047 §4): один
`FILE_ENCRYPTION_KEY` на контур, AES-256-GCM, «версия + nonce + шифртекст».
AAD — адрес значения `{tenant_id}/{таблица}/{id строки}/{колонка}`: шифртекст,
переложенный в чужую строку, колонку или тенант, не расшифруется.

Ключа нет или он не подходит — ERR-FILES-005 (fail-closed): поле не пишется
открытым текстом и не отдаётся пустым молча.
"""

from __future__ import annotations

import uuid

from hospitality.platform.file_content import FileDecryptError, decrypt_object, encrypt_object
from hospitality.shared.config import (
    InvalidFileEncryptionKeyError,
    file_encryption_key_bytes,
    get_settings,
)
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

__all__ = ["decrypt_field", "encrypt_field", "field_aad"]

logger = get_logger(module=__name__)

# Тот же код, что у файлов: ключ один, и рунбук у него один (errors.md).
ERR_FIELD_KEY = "ERR-FILES-005"


def field_aad(tenant_id: uuid.UUID, table: str, row_id: uuid.UUID, column: str) -> str:
    """Адрес значения — AAD шифрования."""
    return f"{tenant_id}/{table}/{row_id}/{column}"


def encrypt_field(plaintext: str, aad: str) -> bytes:
    """Зашифровать значение поля; ключа нет — ERR-FILES-005."""
    return encrypt_object(plaintext.encode(), _key("encrypt"), aad)


def decrypt_field(blob: bytes, aad: str) -> str:
    """Расшифровать значение поля; ключ не тот или шифртекст чужой — ERR-FILES-005."""
    key = _key("decrypt")
    try:
        return decrypt_object(blob, key, aad).decode()
    except FileDecryptError as exc:
        logger.warning("field_decrypt_failed", error_code=ERR_FIELD_KEY)
        raise _unavailable() from exc


def _key(operation: str) -> bytes:
    try:
        key = file_encryption_key_bytes(get_settings())
    except InvalidFileEncryptionKeyError:
        key = None
    if key is None:
        logger.warning(
            "field_encryption_key_missing", error_code=ERR_FIELD_KEY, operation=operation
        )
        raise _unavailable()
    return key


def _unavailable() -> AppError:
    # WARNING, не ERROR: путь гостя и сотрудника, тело запроса не должно уйти
    # в Sentry, пока не закрыт #433 (как в platform/files.py).
    return AppError(code=ERR_FIELD_KEY, message="Документы сейчас недоступны.", status_code=503)

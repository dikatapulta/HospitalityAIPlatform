"""Шифрование файлов на томе приложением (ADR-022 п. 2, spec 0047 §3 шаг 4, FOUNDATION §11).

Формат: `0x01 ‖ nonce(12) ‖ шифротекст+тег(16)` — AES-256-GCM. Первый байт — версия
формата: смена ключа или алгоритма потребует перешифровки, и старые файлы надо будет
отличить. Дополнительные данные шифра — ключ хранения (`{tenant}/{purpose}/{uuid}.{ext}`):
шифротекст, подложенный под чужой ключ — в том числе другого отеля, — не расшифруется.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hospitality.shared.config import get_settings

_FORMAT_VERSION = b"\x01"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class FileDecryptionError(Exception):
    """Шифротекст не расшифровался: другой ключ, чужой ключ хранения или порча байтов."""


def load_file_encryption_key(raw: str) -> bytes | None:
    """`FILE_ENCRYPTION_KEY` → 32 байта; пусто — `None`.

    Текст ошибки не содержит значения: это секрет, а сообщение уходит в лог контейнера.
    """
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error:
        raise ValueError("FILE_ENCRYPTION_KEY is not valid base64") from None
    if len(key) != _KEY_BYTES:
        raise ValueError(f"FILE_ENCRYPTION_KEY must decode to {_KEY_BYTES} bytes")
    return key


def validate_file_encryption_key() -> None:
    """Preflight (`preflight.py`): негодный ключ — отказ старта, пустой — штатно (ADR-022)."""
    try:
        load_file_encryption_key(get_settings().file_encryption_key)
    except ValueError as error:
        raise SystemExit(
            f"{error} (docs/runbooks/secrets.md): сгенерируйте `openssl rand -base64 32` "
            "или оставьте переменную пустой — тогда приём файлов выключен (ERR-FILES-006)."
        ) from None


def encrypt_file(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return _FORMAT_VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, associated_data)


def decrypt_file(key: bytes, blob: bytes, associated_data: bytes) -> bytes:
    if not blob.startswith(_FORMAT_VERSION) or len(blob) < 1 + _NONCE_BYTES + 16:
        raise FileDecryptionError("unknown file encryption format")
    nonce = blob[1 : 1 + _NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[1 + _NONCE_BYTES :], associated_data)
    except InvalidTag:
        raise FileDecryptionError("file authentication tag mismatch") from None

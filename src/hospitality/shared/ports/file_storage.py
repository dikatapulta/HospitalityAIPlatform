"""Порт хранилища файлов (spec 0047, ADR-022, FOUNDATION §8, P-3).

CANONICAL: первый порт kernel'а — образец для следующих (PMS, 1С, платежи).
Канон порта: `typing.Protocol` с async-методами в `shared/ports/<домен>.py`,
исключения порта рядом с ним, адаптеры — `integrations/<домен>/` (Fake
обязателен, ADR-007), общий набор контрактных тестов на все адаптеры (R-7),
выбор адаптера — только composition root (`app.py`, `worker.py`), который
кладёт его сюда `set_file_storage`. Ядро зовёт `get_file_storage()` и не знает,
какой адаптер за ним.

Порт хранит непрозрачные байты по ключу и ничего не знает о тенантах, типах
файлов и шифровании — это работа сервиса `platform/files.py`, единственной
двери модулей к файлам. Ключ — `{tenant_id}/{purpose}/{uuid}.{ext}`: тенант в
ключе — вторая стена к RLS метаданных, а формат проверяется здесь, до любого
адаптера, — ключ с `..` или абсолютным путём не дойдёт до диска.
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

# Тенант — каноническая строка UUID, назначение — `FilePurpose.value`, имя —
# uuid4 hex, расширение — то, что дал сервис после проверки содержимого.
_KEY_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/[a-z_]{1,32}/[0-9a-f]{32}\.[a-z0-9]{1,8}$"
)


class StoredObjectNotFoundError(Exception):
    """Объекта с таким ключом в хранилище нет."""


class FileStorageNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


def build_storage_key(
    tenant_id: uuid.UUID, purpose: str, object_id: uuid.UUID, extension: str
) -> str:
    """Ключ объекта: `{tenant_id}/{purpose}/{uuid}.{ext}` (spec 0047 §3)."""
    return validate_storage_key(f"{tenant_id}/{purpose}/{object_id.hex}.{extension}")


def validate_storage_key(key: str) -> str:
    """Ключ вне формата — `ValueError`; адаптеры зовут это до обращения к носителю."""
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError(f"storage key does not match the canonical format: {key!r}")
    return key


class FileStoragePort(Protocol):
    """Хранилище непрозрачных байтов по ключу.

    Контракт (общие тесты — `tests/test_file_storage_port.py`):
    - `put` пишет атомарно: читатель видит либо старый объект, либо новый целиком;
      повторный `put` того же ключа перезаписывает (P-8);
    - `get` отсутствующего ключа — `StoredObjectNotFoundError`;
    - `delete` отсутствующего ключа — не ошибка (повтор ретеншна безопасен, P-8);
    - ключ вне формата — `ValueError` во всех трёх методах;
    - `check` — health адаптера (§8 правило 1): `True`, если носитель готов писать.
    """

    backend: str

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def check(self) -> bool: ...


_storage: FileStoragePort | None = None


def set_file_storage(storage: FileStoragePort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _storage
    _storage = storage


def get_file_storage() -> FileStoragePort:
    """Адаптер процесса; не положен — `FileStorageNotConfiguredError`."""
    if _storage is None:
        raise FileStorageNotConfiguredError(
            "file storage is not configured: composition root must call set_file_storage()"
        )
    return _storage


def file_storage_or_none() -> FileStoragePort | None:
    """То же без исключения — для `/health/ready`, которому нужен ответ, а не падение."""
    return _storage

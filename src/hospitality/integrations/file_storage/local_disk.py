"""Дисковый адаптер хранилища файлов (ADR-022 п. 1): файлы на томе Docker `files_data`.

Ключ `{tenant}/{purpose}/{uuid}.{ext}` становится путём под корнем `FILE_STORAGE_ROOT`.
Строгий формат ключа (`validate_storage_key`) и есть защита от выхода из корня: `..` и
абсолютный путь в него не проходят.

Запись атомарна: байты пишутся во временный файл рядом и переименовываются — оборванная
запись не оставит полуфайла под боевым именем. Права — `0600`: на томе только шифротекст
(ADR-022 п. 2), но читать его всё равно некому, кроме процесса приложения.

Конструктор диска не касается: `create_app()` строит адаптер и в тестах, и на машине
разработчика, где корня может не быть. Каталоги создаются при первой записи.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from hospitality.shared.ports.file_storage import (
    FileStorageError,
    StoredObjectNotFoundError,
    validate_storage_key,
)


class LocalDiskFileStorageAdapter:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        validate_storage_key(key)
        return self._root / key

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            await asyncio.to_thread(_write_atomically, path, data)
        except OSError as error:
            raise FileStorageError(f"put failed: {error.strerror}") from error

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise StoredObjectNotFoundError(key) from None
        except OSError as error:
            raise FileStorageError(f"get failed: {error.strerror}") from error

    async def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as error:
            raise FileStorageError(f"delete failed: {error.strerror}") from error

    async def check_health(self) -> bool:
        return await asyncio.to_thread(_writable, self._root)


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _writable(root: Path) -> bool:
    """Корень пишется — или его можно создать: первая запись создаст его сама.

    Проверка без побочных эффектов (ничего не создаёт): `/health/ready` опрашивают часто.
    """
    candidate = root.absolute()
    while not candidate.exists():
        if candidate.parent == candidate:
            return False
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)

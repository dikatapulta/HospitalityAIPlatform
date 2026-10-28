"""Дисковый адаптер хранилища файлов (ADR-022, spec 0047): каталог на томе сервера.

Объект — файл `{root}/{tenant_id}/{purpose}/{uuid}.{ext}` с правами 0600,
каталоги — 0700. Запись атомарна: временный файл в том же каталоге и
`os.replace` — читатель не увидит половину файла, упавшая запись не оставит
файла под боевым ключом. Диск — блокирующий ввод-вывод, поэтому каждая
операция уходит в поток (`asyncio.to_thread`) и не держит цикл событий.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from hospitality.shared.ports.file_storage import StoredObjectNotFoundError, validate_storage_key


class LocalDiskFileStorageAdapter:
    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        # Формат ключа уже исключает `..` и абсолютный путь; сверка с корнем —
        # вторая стена на случай, если формат когда-нибудь ослабят.
        path = (self._root / validate_storage_key(key)).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put_sync, self._path(key), bytes(data))

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, self._path(key), key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def check(self) -> bool:
        return await asyncio.to_thread(self._check_sync)

    @staticmethod
    def _put_sync(path: Path, data: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _get_sync(path: Path, key: str) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise StoredObjectNotFoundError(key) from None

    def _check_sync(self) -> bool:
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return False
        return os.access(self._root, os.W_OK | os.X_OK)

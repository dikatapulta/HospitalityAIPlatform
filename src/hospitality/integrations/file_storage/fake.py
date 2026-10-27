"""Fake-адаптер хранилища файлов (ADR-007): байты в памяти процесса.

Для тестов, CI и демо без тома. Ведёт себя как настоящий — проверяет ключ, отвечает
`StoredObjectNotFoundError` на пропавший ключ, удаление идемпотентно — и проходит тот же
контрактный набор (`tests/test_file_storage_contract.py`). Рестарт процесса стирает всё:
на staging и в проде выбирают `local` (`FILE_STORAGE_BACKEND`).
"""

from __future__ import annotations

from hospitality.shared.ports.file_storage import StoredObjectNotFoundError, validate_storage_key


class FakeFileStorageAdapter:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        validate_storage_key(key)
        self._objects[key] = bytes(data)

    async def get(self, key: str) -> bytes:
        validate_storage_key(key)
        try:
            return self._objects[key]
        except KeyError:
            raise StoredObjectNotFoundError(key) from None

    async def delete(self, key: str) -> None:
        validate_storage_key(key)
        self._objects.pop(key, None)

    async def check_health(self) -> bool:
        return True

    def stored_keys(self) -> list[str]:
        """Что лежит сейчас — для тестов и демо (у дискового адаптера аналог — `ls` тома)."""
        return sorted(self._objects)

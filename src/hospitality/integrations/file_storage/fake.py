"""Fake-адаптер хранилища файлов (ADR-007, spec 0047): память процесса.

Обычный адаптер порта `FileStoragePort`: тот же контракт, те же контрактные
тесты, что у дискового. Для dev, CI и тестов; на staging и копии не годится —
у uvicorn с двумя воркерами память у каждого своя, а рестарт её стирает.
"""

from __future__ import annotations

from hospitality.shared.ports.file_storage import StoredObjectNotFoundError, validate_storage_key


class FakeFileStorageAdapter:
    backend = "fake"

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[validate_storage_key(key)] = (bytes(data), content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self._objects[validate_storage_key(key)][0]
        except KeyError:
            raise StoredObjectNotFoundError(key) from None

    async def delete(self, key: str) -> None:
        self._objects.pop(validate_storage_key(key), None)

    async def check(self) -> bool:
        return True

    def keys(self) -> list[str]:
        """Ключи лежащих объектов — для тестов и демо (сколько файлов, чьи)."""
        return sorted(self._objects)

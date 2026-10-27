"""Адаптеры порта хранилища файлов (spec 0047, ADR-022) и выбор по настройкам."""

from __future__ import annotations

from hospitality.integrations.file_storage.fake import FakeFileStorageAdapter
from hospitality.integrations.file_storage.local_disk import LocalDiskFileStorageAdapter
from hospitality.shared.config import Settings
from hospitality.shared.ports.file_storage import FileStoragePort

__all__ = ["FakeFileStorageAdapter", "LocalDiskFileStorageAdapter", "build_file_storage"]


def build_file_storage(settings: Settings) -> FileStoragePort:
    """Адаптер по `FILE_STORAGE_BACKEND` — зовёт только composition root."""
    if settings.file_storage_backend == "local":
        return LocalDiskFileStorageAdapter(settings.file_storage_root)
    return FakeFileStorageAdapter()

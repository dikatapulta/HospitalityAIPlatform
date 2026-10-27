"""Адаптеры порта `FileStoragePort` (spec 0047, ADR-022). Паспорт — README.md рядом."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from hospitality.integrations.file_storage.fake import FakeFileStorageAdapter
from hospitality.integrations.file_storage.local_disk import LocalDiskFileStorageAdapter
from hospitality.shared.ports.file_storage import FileStoragePort


def build_file_storage_adapter(backend: Literal["local", "fake"], root: str) -> FileStoragePort:
    """Адаптер по конфигурации (§8 правило 3) — зовут только composition root'ы."""
    if backend == "fake":
        return FakeFileStorageAdapter()
    return LocalDiskFileStorageAdapter(Path(root))


__all__ = [
    "FakeFileStorageAdapter",
    "LocalDiskFileStorageAdapter",
    "build_file_storage_adapter",
]

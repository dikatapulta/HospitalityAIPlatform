"""Контрактные тесты порта хранилища файлов (R-7, ADR-007, spec 0047 §3).

CANONICAL: один набор тестов на ВСЕ адаптеры порта — параметризацией фикстуры.
Новый адаптер (S3 при переезде) добавляется строкой в `ADAPTERS` и обязан
пройти тот же набор; Fake, не проходящий контракт своего порта, — дефект.
"""

from __future__ import annotations

import stat
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from hospitality.integrations.file_storage import (
    FakeFileStorageAdapter,
    LocalDiskFileStorageAdapter,
)
from hospitality.shared.ports.file_storage import (
    FileStoragePort,
    StoredObjectNotFoundError,
    build_storage_key,
)

ADAPTERS: dict[str, Callable[[Path], FileStoragePort]] = {
    "fake": lambda _root: FakeFileStorageAdapter(),
    "local": lambda root: LocalDiskFileStorageAdapter(root / "files"),
}

_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture(params=sorted(ADAPTERS))
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> FileStoragePort:
    return ADAPTERS[request.param](tmp_path)


def _key(purpose: str = "guest_document", extension: str = "pdf") -> str:
    return build_storage_key(_TENANT, purpose, uuid.uuid4(), extension)


async def test_put_then_get_returns_same_bytes(storage: FileStoragePort) -> None:
    key = _key()
    await storage.put(key, b"\x01encrypted-bytes", "application/pdf")
    assert await storage.get(key) == b"\x01encrypted-bytes"


async def test_put_overwrites_same_key(storage: FileStoragePort) -> None:
    key = _key()
    await storage.put(key, b"first", "application/pdf")
    await storage.put(key, b"second", "application/pdf")
    assert await storage.get(key) == b"second"


async def test_get_missing_key_raises_not_found(storage: FileStoragePort) -> None:
    with pytest.raises(StoredObjectNotFoundError):
        await storage.get(_key())


async def test_delete_is_idempotent(storage: FileStoragePort) -> None:
    key = _key()
    await storage.put(key, b"data", "image/webp")
    await storage.delete(key)
    await storage.delete(key)
    with pytest.raises(StoredObjectNotFoundError):
        await storage.get(key)


async def test_keys_are_independent(storage: FileStoragePort) -> None:
    first, second = _key(), _key("menu_photo", "webp")
    await storage.put(first, b"one", "application/pdf")
    await storage.put(second, b"two", "image/webp")
    await storage.delete(first)
    assert await storage.get(second) == b"two"


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../etc/passwd",
        "/etc/passwd",
        f"{_TENANT}/guest_document/../../../outside.pdf",
        f"{_TENANT}/guest_document/{'a' * 32}.pdf/../x",
        "not-a-tenant/guest_document/" + "a" * 32 + ".pdf",
        f"{_TENANT}/GUEST/" + "a" * 32 + ".pdf",
        "",
    ],
)
async def test_malformed_key_is_rejected_by_every_method(
    storage: FileStoragePort, bad_key: str
) -> None:
    with pytest.raises(ValueError):
        await storage.put(bad_key, b"x", "application/pdf")
    with pytest.raises(ValueError):
        await storage.get(bad_key)
    with pytest.raises(ValueError):
        await storage.delete(bad_key)


async def test_check_reports_ready(storage: FileStoragePort) -> None:
    assert await storage.check() is True


def test_key_carries_tenant_and_purpose() -> None:
    object_id = uuid.uuid4()
    key = build_storage_key(_TENANT, "guest_document", object_id, "pdf")
    assert key == f"{_TENANT}/guest_document/{object_id.hex}.pdf"


# --- Свойства дискового адаптера сверх контракта ----------------------------


async def test_local_files_are_private_and_under_root(tmp_path: Path) -> None:
    root = tmp_path / "files"
    storage = LocalDiskFileStorageAdapter(root)
    key = _key()

    await storage.put(key, b"secret", "application/pdf")

    path = root / key
    assert path.read_bytes() == b"secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Временных файлов записи не остаётся.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


async def test_local_check_fails_when_root_is_not_a_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "files"
    blocker.write_text("файл на месте каталога")
    assert await LocalDiskFileStorageAdapter(blocker).check() is False

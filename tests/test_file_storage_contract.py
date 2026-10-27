"""Контрактный набор порта `FileStoragePort` (R-7, ADR-007; spec 0047 §7).

Один набор — на каждый адаптер: Fake обязан вести себя как настоящий, иначе тесты на нём
доказывают не то. Новый адаптер (S3 при втором узле) добавляется в `adapter` ниже.
"""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hospitality.integrations.file_storage import (
    FakeFileStorageAdapter,
    LocalDiskFileStorageAdapter,
    build_file_storage_adapter,
)
from hospitality.shared.ports.file_storage import (
    FileStorageError,
    FileStoragePort,
    StoredObjectNotFoundError,
    validate_storage_key,
)


def _key(extension: str = "webp") -> str:
    return f"{uuid.uuid4()}/guest_document/{uuid.uuid4()}.{extension}"


@pytest.fixture(params=["fake", "local"])
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[FileStoragePort]:
    if request.param == "fake":
        yield FakeFileStorageAdapter()
    else:
        yield LocalDiskFileStorageAdapter(tmp_path / "files")


async def test_put_then_get_returns_same_bytes(adapter: FileStoragePort) -> None:
    key = _key()
    await adapter.put(key, b"\x00ciphertext\xff")

    assert await adapter.get(key) == b"\x00ciphertext\xff"


async def test_put_overwrites_existing_key(adapter: FileStoragePort) -> None:
    key = _key()
    await adapter.put(key, b"first")
    await adapter.put(key, b"second")

    assert await adapter.get(key) == b"second"


async def test_get_missing_key_raises_not_found(adapter: FileStoragePort) -> None:
    with pytest.raises(StoredObjectNotFoundError):
        await adapter.get(_key())


async def test_delete_removes_and_is_idempotent(adapter: FileStoragePort) -> None:
    key = _key()
    await adapter.put(key, b"bytes")

    await adapter.delete(key)
    await adapter.delete(key)  # повтор — не ошибка (P-8)

    with pytest.raises(StoredObjectNotFoundError):
        await adapter.get(key)


@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc/passwd",
        "/etc/passwd",
        f"{uuid.uuid4()}/guest_document/../../x.pdf",
        f"{uuid.uuid4()}/Guest/{uuid.uuid4()}.pdf",
        f"{uuid.uuid4()}/guest_document/{uuid.uuid4()}",
        "",
    ],
)
async def test_bad_key_rejected_before_storage(adapter: FileStoragePort, bad_key: str) -> None:
    with pytest.raises(ValueError):
        await adapter.put(bad_key, b"x")
    with pytest.raises(ValueError):
        await adapter.get(bad_key)
    with pytest.raises(ValueError):
        await adapter.delete(bad_key)


async def test_health_ok_on_fresh_storage(adapter: FileStoragePort) -> None:
    assert await adapter.check_health() is True


def test_valid_key_shape_accepted() -> None:
    validate_storage_key(_key("pdf"))


# --- Только дисковый адаптер: то, чего у памяти нет --------------------------------------


async def test_local_disk_writes_owner_only_file_under_root(tmp_path: Path) -> None:
    root = tmp_path / "files"
    key = _key()
    await LocalDiskFileStorageAdapter(root).put(key, b"bytes")

    path = root / key
    assert path.read_bytes() == b"bytes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".*.part")), "временный файл записи не убран"


async def test_local_disk_health_false_when_root_not_writable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root пишет и в каталог 0500 — проверка прав под ним бессмысленна")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert await LocalDiskFileStorageAdapter(locked / "files").check_health() is False
    finally:
        locked.chmod(0o700)


async def test_local_disk_wraps_os_errors(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.write_text("файл на месте каталога")  # mkdir корня невозможен

    with pytest.raises(FileStorageError):
        await LocalDiskFileStorageAdapter(root).put(_key(), b"bytes")


def test_builder_picks_adapter_by_backend(tmp_path: Path) -> None:
    assert isinstance(build_file_storage_adapter("fake", ""), FakeFileStorageAdapter)
    assert isinstance(
        build_file_storage_adapter("local", str(tmp_path)), LocalDiskFileStorageAdapter
    )

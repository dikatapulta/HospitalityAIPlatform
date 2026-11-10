"""Контрактные тесты порта антивируса (R-7, ADR-007, spec 0075 §3, §6).

Канон — `tests/test_file_storage_port.py`: один набор на все адаптеры
параметризацией фикстуры. Адаптер clamd говорит здесь с тестовым сервером
протокола в том же процессе: он собирает куски `INSTREAM` и выносит вердикт
правилом Fake-адаптера — то есть оба адаптера обязаны отвечать одинаково.
Настоящий clamd в CI не поднимается (1,2 ГБ памяти и загрузка баз): протокол
сверен с ClamAV 1.5.4 руками, ответы ниже — его дословные строки.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest

from hospitality.integrations.virus_scan import (
    ClamdVirusScanAdapter,
    FakeVirusScanAdapter,
    build_virus_scanner,
)
from hospitality.integrations.virus_scan.fake import EICAR_TEST_FILE
from hospitality.shared.config import Settings
from hospitality.shared.ports import virus_scan
from hospitality.shared.ports.virus_scan import (
    VirusScannerNotConfiguredError,
    VirusScanPort,
    VirusScanUnavailableError,
    get_virus_scanner,
)

_PDF_WITH_EICAR_INSIDE = b"%PDF-1.4\n" + EICAR_TEST_FILE + b"\n%%EOF\n"


@dataclass
class FakeClamd:
    """Сервер протокола clamd: `zPING`, `zINSTREAM`; `mode` — как ему себя вести."""

    mode: str = "normal"  # normal | error | close | silent
    chunks: list[int] = field(default_factory=list)
    port: int = 0
    _verdict: FakeVirusScanAdapter = field(default_factory=FakeVirusScanAdapter)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            command = await reader.readuntil(b"\0")
            if self.mode == "close":
                return
            if self.mode == "silent":
                await asyncio.sleep(3600)
            if command == b"zPING\0":
                writer.write(b"PONG\0")
            elif command == b"zINSTREAM\0":
                writer.write(await self._instream(reader))
            await writer.drain()
        finally:
            writer.close()

    async def _instream(self, reader: asyncio.StreamReader) -> bytes:
        data = b""
        while True:
            (size,) = struct.unpack("!L", await reader.readexactly(4))
            self.chunks.append(size)
            if size == 0:
                break
            data += await reader.readexactly(size)
        if self.mode == "error":
            return b"INSTREAM size limit exceeded. ERROR\0"
        result = await self._verdict.scan(data)
        if result.infected:
            return f"stream: {result.signature} FOUND\0".encode()
        return b"stream: OK\0"


@pytest.fixture
async def clamd() -> AsyncIterator[FakeClamd]:
    fake = FakeClamd()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    try:
        yield fake
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=1)


Factory = Callable[[FakeClamd], VirusScanPort]
ADAPTERS: dict[str, Factory] = {
    "fake": lambda _clamd: FakeVirusScanAdapter(),
    "clamav": lambda clamd: ClamdVirusScanAdapter("127.0.0.1", clamd.port),
}


@pytest.fixture(params=sorted(ADAPTERS))
def scanner(request: pytest.FixtureRequest, clamd: FakeClamd) -> VirusScanPort:
    return ADAPTERS[request.param](clamd)


# --- контракт: оба адаптера ---------------------------------------------------


async def test_clean_file_is_clean(scanner: VirusScanPort) -> None:
    result = await scanner.scan(b"%PDF-1.4\nBIN 123456789012\n%%EOF\n")
    assert (result.infected, result.signature) == (False, None)


@pytest.mark.parametrize("payload", [EICAR_TEST_FILE, EICAR_TEST_FILE + b"\r\n"])
async def test_eicar_test_file_is_infected(scanner: VirusScanPort, payload: bytes) -> None:
    result = await scanner.scan(payload)
    assert result.infected
    assert result.signature


async def test_eicar_inside_pdf_is_not_the_test_file(scanner: VirusScanPort) -> None:
    # Так отвечает ClamAV 1.5.4: тестовая строка — только в начале файла.
    assert not (await scanner.scan(_PDF_WITH_EICAR_INSIDE)).infected


async def test_empty_file_is_clean(scanner: VirusScanPort) -> None:
    assert not (await scanner.scan(b"")).infected


async def test_check_is_true_when_scanner_answers(scanner: VirusScanPort) -> None:
    assert await scanner.check() is True


# --- Fake: свои сигнатуры и «антивирус лежит» ----------------------------------


async def test_fake_custom_signature_matches_anywhere() -> None:
    fake = FakeVirusScanAdapter()
    fake.add_signature(b"MALWARE-MARK", "Test.Marker")
    result = await fake.scan(b"%PDF-1.4\n MALWARE-MARK \n")
    assert (result.infected, result.signature) == (True, "Test.Marker")


async def test_fake_unavailable_raises_and_fails_check() -> None:
    fake = FakeVirusScanAdapter()
    fake.unavailable = True
    with pytest.raises(VirusScanUnavailableError):
        await fake.scan(b"x")
    assert await fake.check() is False


# --- clamd: протокол и отказы --------------------------------------------------


async def test_clamd_streams_in_chunks_and_ends_with_zero(clamd: FakeClamd) -> None:
    adapter = ClamdVirusScanAdapter("127.0.0.1", clamd.port)
    await adapter.scan(bytes(150 * 1024))
    assert clamd.chunks == [64 * 1024, 64 * 1024, 22 * 1024, 0]


async def test_clamd_error_reply_is_unavailable(clamd: FakeClamd) -> None:
    clamd.mode = "error"
    with pytest.raises(VirusScanUnavailableError, match="ERROR"):
        await ClamdVirusScanAdapter("127.0.0.1", clamd.port).scan(b"x")


async def test_clamd_closing_without_reply_is_unavailable(clamd: FakeClamd) -> None:
    clamd.mode = "close"
    adapter = ClamdVirusScanAdapter("127.0.0.1", clamd.port)
    with pytest.raises(VirusScanUnavailableError):
        await adapter.scan(b"x")
    assert await adapter.check() is False


async def test_clamd_timeout_is_unavailable(clamd: FakeClamd) -> None:
    clamd.mode = "silent"
    adapter = ClamdVirusScanAdapter("127.0.0.1", clamd.port, timeout_seconds=0.2)
    with pytest.raises(VirusScanUnavailableError, match="timed out"):
        await adapter.scan(b"x")


async def test_clamd_refused_connection_is_unavailable() -> None:
    server = await asyncio.start_server(_never_called, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    adapter = ClamdVirusScanAdapter("127.0.0.1", port)
    with pytest.raises(VirusScanUnavailableError, match="connection failed"):
        await adapter.scan(b"x")
    assert await adapter.check() is False


async def _never_called(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
    raise AssertionError("closed server accepted a connection")


# --- выбор адаптера и порт -----------------------------------------------------


def test_backend_setting_selects_adapter() -> None:
    assert isinstance(
        build_virus_scanner(Settings(virus_scan_backend="fake")), FakeVirusScanAdapter
    )
    clamav = build_virus_scanner(Settings(virus_scan_backend="clamav"))
    assert isinstance(clamav, ClamdVirusScanAdapter)
    assert clamav.backend == "clamav"


def test_port_without_adapter_is_a_build_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(virus_scan, "_scanner", None)
    with pytest.raises(VirusScannerNotConfiguredError):
        get_virus_scanner()

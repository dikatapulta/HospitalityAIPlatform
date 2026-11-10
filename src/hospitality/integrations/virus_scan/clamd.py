"""Адаптер антивируса ClamAV (spec 0075): демон clamd по TCP, протокол INSTREAM.

Протокол (документация clamd, сверено с ClamAV 1.5.4): команда `zINSTREAM\\0`,
затем куски `<длина: 4 байта big-endian><байты>`, конец — кусок нулевой длины.
Ответ — одна строка с `\\0` на конце: `stream: OK`, `stream: <сигнатура> FOUND`
или `<текст> ERROR` (например, файл больше `StreamMaxLength`). `zPING\\0` —
`PONG`. Соединение — новое на каждый вызов: clamd держит свой пул потоков, а
веб-процессу нечего беречь между редкими загрузками.

Нет вердикта (нет соединения, обрыв, таймаут, `ERROR`) — `VirusScanUnavailableError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

from hospitality.shared.ports.virus_scan import CLEAN, ScanResult, VirusScanUnavailableError

SCAN_TIMEOUT_SECONDS = 30.0
_CHUNK_BYTES = 64 * 1024
_REPLY_LIMIT_BYTES = 4096
_FOUND_SUFFIX = " FOUND"
_STREAM_PREFIX = "stream:"


class ClamdVirusScanAdapter:
    backend = "clamav"

    def __init__(
        self, host: str, port: int, *, timeout_seconds: float = SCAN_TIMEOUT_SECONDS
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_seconds

    async def scan(self, data: bytes) -> ScanResult:
        reply = await self._call(b"zINSTREAM\0", data)
        if reply.endswith(_FOUND_SUFFIX):
            name = reply.removeprefix(_STREAM_PREFIX).removesuffix(_FOUND_SUFFIX).strip()
            return ScanResult(infected=True, signature=name or "unknown")
        if reply == f"{_STREAM_PREFIX} OK":
            return CLEAN
        raise VirusScanUnavailableError(f"clamd answered: {reply[:200]}")

    async def check(self) -> bool:
        try:
            return await self._call(b"zPING\0", None) == "PONG"
        except VirusScanUnavailableError:
            return False

    async def _call(self, command: bytes, data: bytes | None) -> str:
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_connection(
                    self._host, self._port, limit=_REPLY_LIMIT_BYTES
                )
                try:
                    writer.write(command)
                    if data is not None:
                        for start in range(0, len(data), _CHUNK_BYTES):
                            chunk = data[start : start + _CHUNK_BYTES]
                            writer.write(struct.pack("!L", len(chunk)) + chunk)
                            await writer.drain()
                        writer.write(struct.pack("!L", 0))
                    await writer.drain()
                    reply = await reader.readuntil(b"\0")
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except TimeoutError as exc:
            raise VirusScanUnavailableError(f"clamd timed out after {self._timeout} s") from exc
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise VirusScanUnavailableError(f"clamd connection failed: {exc!r}") from exc
        return reply[:-1].decode(errors="replace").strip()

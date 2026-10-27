"""Проверка загруженного файла: тип по содержимому и разбор в дочернем процессе (spec 0047 §3).

Тип определяется по первым байтам (сигнатура), а не по расширению и не по `Content-Type`,
которые присылает кто угодно. Сравнение префикса — не разбор, поэтому оно здесь, в
веб-процессе; всё, что разбирает содержимое, уходит в `platform/file_sanitizer.py`
отдельным процессом (ADR-022 п. 3).
"""

from __future__ import annotations

import asyncio
import enum
import sys

from hospitality.platform.file_sanitizer import EXIT_REJECTED

# Числа — spec 0047 §3.
SANITIZER_TIMEOUT_SECONDS = 15.0
_MAX_PARALLEL_SANITIZERS = 2


class FileKind(enum.StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    DOCX = "docx"


class FileInspectionError(Exception):
    """Файл не прошёл разбор; `reason` — короткая причина для лога и выбора кода ошибки."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sniff_kind(data: bytes) -> FileKind | None:
    """Белый список сигнатур; всё прочее (SVG, HTML, исполняемые, GIF, HEIC) — `None`."""
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind.IMAGE
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return FileKind.IMAGE
    if data.startswith(b"%PDF-"):
        return FileKind.PDF
    if data.startswith(b"PK\x03\x04"):
        return FileKind.DOCX
    return None


async def sanitize(kind: FileKind, data: bytes, image_max_side: int) -> bytes:
    """Байты, которые будут храниться: WebP для картинки, исходник для PDF и DOCX.

    PDF не разбирается вовсе: хранится как есть и отдаётся только скачиванием (spec 0047 §4).
    """
    if kind is FileKind.IMAGE:
        return await _run_sanitizer(["image", str(image_max_side)], data)
    if kind is FileKind.DOCX:
        await _run_sanitizer(["docx"], data)
    return data


_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None


def _sanitizer_slots() -> asyncio.Semaphore:
    """Семафор текущего цикла событий: семафор привязывается к циклу, а в тестах их много."""
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        _slots, _slots_loop = asyncio.Semaphore(_MAX_PARALLEL_SANITIZERS), loop
    return _slots


async def _run_sanitizer(arguments: list[str], data: bytes) -> bytes:
    async with _sanitizer_slots():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "hospitality.platform.file_sanitizer",
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(data), timeout=SANITIZER_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise FileInspectionError("timeout") from None
    if process.returncode == 0:
        return stdout
    if process.returncode == EXIT_REJECTED:
        reason = stderr.decode("ascii", "replace").strip().splitlines()[:1]
        raise FileInspectionError(reason[0] if reason else "rejected")
    # Убит лимитом памяти или процессора, упал декодер: файл не принят — причина в лог.
    raise FileInspectionError(f"sanitizer_exit_{process.returncode}")

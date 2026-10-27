"""Изоляция обработки загруженного файла (FOUNDATION §11, spec 0047 §4, ADR-022).

Проверка и перекодирование (`file_content.prepare_upload`) идут не в веб-процессе,
а в дочернем: `python -I -m hospitality.platform.file_sandbox <назначение>`, файл —
в stdin. Ребёнок получает пустое окружение (ни ключей, ни пароля БД), лимит
памяти и процессорного времени (Linux, `resource`), а родитель ждёт его не
дольше таймаута и держит не больше двух одновременно. Ошибка декодера Pillow
или «бомба» роняет ребёнка, а не процесс, обслуживающий гостей.

Ответ ребёнка родитель не принимает на веру: строка-заголовок JSON — только
вид файла или причина отказа; тип и расширение родитель берёт из своей
таблицы, PDF и DOCX сохраняет из своих байтов, у картинки проверяет подпись
WebP и сам считает sha256.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import weakref
from typing import Any

from hospitality.platform.file_content import (
    KIND_FORMATS,
    FileKind,
    FilePurpose,
    FileRejectedError,
    PreparedFile,
    RejectReason,
    prepare_upload,
)

SANDBOX_CONCURRENCY = 2
SANDBOX_TIMEOUT_SECONDS = 30.0
_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_CPU_LIMIT_SECONDS = 15
_STDERR_TAIL = 500

_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


class SandboxFailedError(Exception):
    """Ребёнок умер, не ответив (лимит, сигнал, сломанный образ) — не вердикт о файле."""


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = _semaphores[loop] = asyncio.Semaphore(SANDBOX_CONCURRENCY)
    return semaphore


async def prepare_upload_isolated(purpose: FilePurpose, data: bytes) -> PreparedFile:
    """`prepare_upload` в дочернем процессе.

    Отказ по содержимому — `FileRejectedError`; ребёнок умер без ответа — `SandboxFailedError`.
    """
    async with _semaphore():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "hospitality.platform.file_sandbox",
            purpose.value,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(data), timeout=SANDBOX_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise FileRejectedError(RejectReason.TOO_LARGE, "sandbox timeout") from None
    header_line, _, body = stdout.partition(b"\n")
    try:
        header: dict[str, Any] = json.loads(header_line)
    except ValueError:
        raise SandboxFailedError(
            f"exit {process.returncode}: {stderr[-_STDERR_TAIL:].decode(errors='replace')}"
        ) from None
    if "rejected" in header:
        raise FileRejectedError(RejectReason(header["rejected"]), "sandbox verdict")
    kind = FileKind(header["kind"])
    if kind is FileKind.IMAGE:
        if not (body.startswith(b"RIFF") and body[8:12] == b"WEBP"):
            raise SandboxFailedError("sandbox returned an image that is not WebP")
        stored = body
    else:
        stored = data
    content_type, extension = KIND_FORMATS[kind]
    return PreparedFile(
        kind=kind,
        data=stored,
        content_type=content_type,
        extension=extension,
        sha256=hashlib.sha256(stored).hexdigest(),
    )


def _apply_limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS))
    except (ImportError, ValueError, OSError):
        # macOS не даёт ставить RLIMIT_AS — локальная разработка идёт без лимита;
        # в контейнере (Linux) лимиты стоят.
        pass


def _answer(header: dict[str, str], body: bytes = b"") -> None:
    sys.stdout.buffer.write(json.dumps(header).encode() + b"\n" + body)
    sys.stdout.buffer.flush()


def _child_main() -> None:  # pragma: no cover — исполняется в дочернем процессе
    _apply_limits()
    purpose = FilePurpose(sys.argv[1])
    data = sys.stdin.buffer.read()
    try:
        prepared = prepare_upload(purpose, data)
    except FileRejectedError as exc:
        _answer({"rejected": exc.reason.value})
        return
    except MemoryError:
        _answer({"rejected": RejectReason.TOO_LARGE.value})
        return
    body = prepared.data if prepared.kind is FileKind.IMAGE else b""
    _answer({"kind": prepared.kind.value}, body)


if __name__ == "__main__":  # pragma: no cover
    _child_main()

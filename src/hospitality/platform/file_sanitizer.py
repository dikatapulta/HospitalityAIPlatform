"""Дочерний процесс разбора недоверенного файла (ADR-022 п. 3, spec 0047 §3 шаг 3).

Всё, что разбирает содержимое загруженного файла, идёт здесь, а не в веб-процессе
(FOUNDATION §11 «изоляция обработки»): картинка-бомба или уязвимость декодера убивает этот
короткоживущий процесс с потолками памяти и процессора, а не процесс с сессиями гостей.
Запускает его `platform/file_inspection.py`:

    python -I -m hospitality.platform.file_sanitizer image <max_side>
    python -I -m hospitality.platform.file_sanitizer docx

stdin — байты файла; stdout — WebP (`image`) или ничего (`docx`). Код возврата: 0 —
принято, 3 — отвергнуто (первая строка stderr — причина), прочее — сбой разбора.

Модуль намеренно не импортирует ничего из платформы: дочерний процесс поднимается быстро и
не тянет за собой SQLAlchemy и настройки.
"""

from __future__ import annotations

import io
import sys
import warnings
import zipfile

EXIT_REJECTED = 3
# Числа — spec 0047 §3.
MAX_IMAGE_PIXELS = 50_000_000
WEBP_QUALITY = 85
MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
CPU_LIMIT_SECONDS = 10
# Оглавление DOCX — килобайты; больше — не документ Word, а повод распаковать бомбу.
_MAX_CONTENT_TYPES_BYTES = 1024 * 1024
_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")


class FileRejectedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover — Windows; прод и CI — Linux
        return
    for limit, value in (
        (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES),
        (resource.RLIMIT_CPU, CPU_LIMIT_SECONDS),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):  # pragma: no cover — macOS не даёт RLIMIT_AS
            continue


def transcode_image(data: bytes, max_side: int) -> bytes:
    """JPEG/PNG/WebP → WebP: поворот по EXIF, сторона ≤ `max_side`, без метаданных."""
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        # `formats` — белый список декодеров: ни EPS (Ghostscript), ни иной экзотики.
        with Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS) as source:
            source.draft("RGB", (max_side, max_side))  # JPEG декодируется сразу уменьшенным
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            has_alpha = image.mode in ("RGBA", "LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            mode = "RGBA" if has_alpha else "RGB"
            pixels = image.convert(mode)
            # Новая картинка из голых пикселей: EXIF, ICC и прочее `info` не переедут.
            clean = Image.frombytes(mode, pixels.size, pixels.tobytes())
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise FileRejectedError("too_many_pixels") from None
    except Exception:
        # Любой отказ декодера — «не картинка» для этого назначения, причин не различаем.
        raise FileRejectedError("undecodable") from None
    output = io.BytesIO()
    clean.save(output, "WEBP", quality=WEBP_QUALITY)
    return output.getvalue()


def inspect_docx(data: bytes) -> None:
    """DOCX без макросов: оглавление ZIP, без распаковки содержимого документа."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise FileRejectedError("not_docx")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise FileRejectedError("macros")
            content_types = archive.getinfo("[Content_Types].xml")
            if content_types.file_size > _MAX_CONTENT_TYPES_BYTES:
                raise FileRejectedError("not_docx")
            if b"macroenabled" in archive.read(content_types).lower():
                raise FileRejectedError("macros")
    except FileRejectedError:
        raise
    except Exception:
        raise FileRejectedError("not_docx") from None


def main(arguments: list[str]) -> int:
    _limit_resources()
    data = sys.stdin.buffer.read()
    try:
        if arguments[:1] == ["image"] and len(arguments) == 2:
            output = transcode_image(data, int(arguments[1]))
        elif arguments == ["docx"]:
            inspect_docx(data)
            output = b""
        else:
            print("usage: file_sanitizer image <max_side> | docx", file=sys.stderr)
            return 2
    except FileRejectedError as rejection:
        print(rejection.reason, file=sys.stderr)
        return EXIT_REJECTED
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа дочернего процесса
    raise SystemExit(main(sys.argv[1:]))

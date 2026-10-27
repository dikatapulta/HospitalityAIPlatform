"""Образцы файлов для тестов хранилища (spec 0047 §9): собираются в памяти, без бинарников в репо."""

from __future__ import annotations

import io
import zipfile

from PIL import Image

# EXIF-тег ориентации и GPS-блок: ровно то, что не должно пережить перекодирование.
_ORIENTATION_TAG = 0x0112
_GPS_IFD_TAG = 0x8825


def png_with_exif(width: int = 3000, height: int = 1500) -> bytes:
    image = Image.new("RGB", (width, height), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[_GPS_IFD_TAG] = {1: "N", 2: (43.0, 15.0, 0.0)}
    output = io.BytesIO()
    image.save(output, "PNG", exif=exif.tobytes())
    return output.getvalue()


def jpeg_rotated_by_exif(width: int = 400, height: int = 200) -> bytes:
    """Ширина больше высоты, но EXIF велит повернуть на 90° — после поворота наоборот."""
    image = Image.new("RGB", (width, height), (10, 120, 200))
    exif = Image.Exif()
    exif[_ORIENTATION_TAG] = 6
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif.tobytes())
    return output.getvalue()


def png_with_alpha() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(output, "PNG")
    return output.getvalue()


def huge_png(width: int = 8000, height: int = 7000) -> bytes:
    """56 Мп однотонной картинки: файл маленький, а пикселей больше предела."""
    output = io.BytesIO()
    Image.new("1", (width, height), 0).save(output, "PNG")
    return output.getvalue()


def docx(*, extra_names: tuple[str, ...] = (), content_types: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            content_types
            or (
                '<Types><Override PartName="/word/document.xml" ContentType='
                '"application/vnd.openxmlformats-officedocument.wordprocessingml.'
                'document.main+xml"/></Types>'
            ),
        )
        archive.writestr("word/document.xml", "<w:document/>")
        for name in extra_names:
            archive.writestr(name, b"\x00binary")
    return output.getvalue()


def macro_enabled_docx() -> bytes:
    return docx(
        content_types=(
            '<Types><Override PartName="/word/document.xml" ContentType='
            '"application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
        )
    )


def plain_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("readme.txt", "not a document")
    return output.getvalue()


PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

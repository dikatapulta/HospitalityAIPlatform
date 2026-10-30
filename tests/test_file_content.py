"""Проверка содержимого загружаемого файла и шифрование (spec 0047 §4–§5, §9).

Чистые функции `platform/file_content.py`: без БД и хранилища. Образцы файлов
собираются здесь же (Pillow, zipfile) — ими пользуются и тесты сервиса.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import zipfile

import pytest
from PIL import Image, ImageCms

from hospitality.platform import file_sandbox
from hospitality.platform.file_content import (
    DOCX_CONTENT_TYPE,
    FileDecryptError,
    FileKind,
    FilePurpose,
    FileRejectedError,
    RejectReason,
    decrypt_object,
    encrypt_object,
    prepare_upload,
)
from hospitality.platform.file_sandbox import SandboxFailedError, prepare_upload_isolated

_GPS_IFD = 0x8825
_ORIENTATION = 0x0112
_KEY = bytes(32)
_STORAGE_KEY = "00000000-0000-0000-0000-000000000001/guest_document/" + "a" * 32 + ".pdf"


def make_png(size: tuple[int, int] = (64, 48), *, with_exif: bool = False) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"  # Make
        exif.get_ifd(_GPS_IFD)[2] = (43.0, 15.0, 0.0)  # GPSLatitude — Алматы
        image.save(out, format="PNG", exif=exif)
    else:
        image.save(out, format="PNG")
    return out.getvalue()


def make_jpeg_with_gps(size: tuple[int, int], *, orientation: int = 1) -> bytes:
    image = Image.new("RGB", size, (30, 120, 200))
    exif = Image.Exif()
    exif[_ORIENTATION] = orientation
    exif.get_ifd(_GPS_IFD)[2] = (43.0, 15.0, 0.0)
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def make_docx(*, extra_entries: dict[str, bytes] | None = None, macro_type: bool = False) -> bytes:
    main_type = (
        "application/vnd.ms-word.document.macroEnabled.main+xml"
        if macro_type
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    content_types = (
        '<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
        f'ContentType="{main_type}"/></Types>'
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", b"<w:document>BIN 123456789012</w:document>")
        for name, data in (extra_entries or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


def _webp_of(prepared_data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(prepared_data))
    assert image.format == "WEBP"
    return image


def test_png_with_exif_becomes_webp_without_metadata() -> None:
    source = make_png((3000, 2000), with_exif=True)
    assert Image.open(io.BytesIO(source)).getexif().get_ifd(_GPS_IFD)  # GPS в исходнике есть

    prepared = prepare_upload(FilePurpose.MENU_PHOTO, source)

    assert (prepared.kind, prepared.content_type, prepared.extension) == (
        FileKind.IMAGE,
        "image/webp",
        "webp",
    )
    image = _webp_of(prepared.data)
    assert image.size == (960, 640)
    assert len(image.getexif()) == 0
    assert "exif" not in image.info and "xmp" not in image.info


def test_document_photo_keeps_legible_size_and_orientation_drops_gps() -> None:
    # orientation=6 — телефон держали вертикально: после поворота высота больше ширины.
    source = make_jpeg_with_gps((4000, 3000), orientation=6)

    prepared = prepare_upload(FilePurpose.GUEST_DOCUMENT, source)

    image = _webp_of(prepared.data)
    assert image.size == (1200, 1600)
    assert len(image.getexif()) == 0


@pytest.mark.parametrize(
    ("payload", "purpose"),
    [
        pytest.param(b"just text renamed to photo.jpg", FilePurpose.MENU_PHOTO, id="text-as-jpg"),
        pytest.param(
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            FilePurpose.GUEST_DOCUMENT,
            id="svg",
        ),
        pytest.param(
            b"<html><script>alert(1)</script></html>", FilePurpose.GUEST_DOCUMENT, id="html"
        ),
        pytest.param(b"MZ\x90\x00\x03", FilePurpose.GUEST_DOCUMENT, id="exe"),
        pytest.param(b"%PDF-1.7 menu as pdf", FilePurpose.MENU_PHOTO, id="pdf-as-menu-photo"),
        pytest.param(make_docx(), FilePurpose.REQUEST_PHOTO, id="docx-as-request-photo"),
    ],
)
def test_unsupported_content_is_rejected(payload: bytes, purpose: FilePurpose) -> None:
    with pytest.raises(FileRejectedError) as caught:
        prepare_upload(purpose, payload)
    assert caught.value.reason is RejectReason.UNSUPPORTED_TYPE


def test_zip_that_is_not_docx_is_rejected() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("payload.exe", b"MZ")
    with pytest.raises(FileRejectedError) as caught:
        prepare_upload(FilePurpose.GUEST_DOCUMENT, out.getvalue())
    assert caught.value.reason is RejectReason.UNSUPPORTED_TYPE


def test_pdf_and_docx_are_stored_as_is() -> None:
    pdf = b"%PDF-1.4\n% requisites\n"
    docx = make_docx()

    prepared_pdf = prepare_upload(FilePurpose.GUEST_DOCUMENT, pdf)
    prepared_docx = prepare_upload(FilePurpose.GUEST_DOCUMENT, docx)

    assert (prepared_pdf.data, prepared_pdf.content_type) == (pdf, "application/pdf")
    assert (prepared_docx.data, prepared_docx.content_type) == (docx, DOCX_CONTENT_TYPE)


@pytest.mark.parametrize(
    "docx",
    [
        pytest.param(make_docx(extra_entries={"word/vbaProject.bin": b"\xd0\xcf"}), id="vba"),
        pytest.param(make_docx(extra_entries={"customUI/VBAPROJECT.BIN": b"x"}), id="vba-case"),
        pytest.param(make_docx(macro_type=True), id="docm-content-type"),
    ],
)
def test_docx_with_macros_is_rejected(docx: bytes) -> None:
    with pytest.raises(FileRejectedError) as caught:
        prepare_upload(FilePurpose.GUEST_DOCUMENT, docx)
    assert caught.value.reason is RejectReason.MACROS


def test_size_limits_by_kind() -> None:
    eleven_mb_pdf = b"%PDF-1.4" + bytes(11 * 1024 * 1024)
    six_mb_pdf = b"%PDF-1.4" + bytes(6 * 1024 * 1024)
    six_mb_photo = bytes(6 * 1024 * 1024)

    for payload, purpose in (
        (eleven_mb_pdf, FilePurpose.GUEST_DOCUMENT),
        (six_mb_photo, FilePurpose.MENU_PHOTO),
        (six_mb_photo, FilePurpose.GUEST_DOCUMENT),
    ):
        with pytest.raises(FileRejectedError) as caught:
            prepare_upload(purpose, payload)
        assert caught.value.reason is RejectReason.TOO_LARGE

    assert prepare_upload(FilePurpose.GUEST_DOCUMENT, six_mb_pdf).kind is FileKind.PDF


def test_decompression_bomb_is_rejected_before_decoding() -> None:
    # 8000×8000 одного цвета — PNG в сотни килобайт, в памяти — 192 МБ.
    bomb = make_png((8000, 8000))
    assert len(bomb) < 5 * 1024 * 1024
    with pytest.raises(FileRejectedError) as caught:
        prepare_upload(FilePurpose.MENU_PHOTO, bomb)
    assert caught.value.reason is RejectReason.TOO_LARGE


def test_encryption_round_trip_is_bound_to_key_and_storage_key() -> None:
    blob = encrypt_object(b"BIN 123456789012", _KEY, _STORAGE_KEY)

    assert b"123456789012" not in blob
    assert decrypt_object(blob, _KEY, _STORAGE_KEY) == b"BIN 123456789012"
    with pytest.raises(FileDecryptError):
        decrypt_object(blob, bytes([1]) * 32, _STORAGE_KEY)
    # Тот же шифртекст под чужим ключом объекта (другой тенант) не расшифруется.
    foreign = "00000000-0000-0000-0000-000000000002" + _STORAGE_KEY[36:]
    with pytest.raises(FileDecryptError):
        decrypt_object(blob, _KEY, foreign)


# --- Песочница: та же проверка в дочернем процессе (FOUNDATION §11) ----------


async def test_sandbox_reencodes_image_in_child_process() -> None:
    prepared = await prepare_upload_isolated(FilePurpose.MENU_PHOTO, make_png((2000, 1000)))

    assert (prepared.kind, prepared.content_type, prepared.extension) == (
        FileKind.IMAGE,
        "image/webp",
        "webp",
    )
    assert _webp_of(prepared.data).size == (960, 480)


async def test_sandbox_returns_documents_from_parent_bytes() -> None:
    pdf = b"%PDF-1.4\n% requisites\n"
    prepared = await prepare_upload_isolated(FilePurpose.GUEST_DOCUMENT, pdf)
    assert (prepared.kind, prepared.data) == (FileKind.PDF, pdf)


async def test_sandbox_relays_rejection_reason() -> None:
    with pytest.raises(FileRejectedError) as caught:
        await prepare_upload_isolated(FilePurpose.MENU_PHOTO, b"<svg/>")
    assert caught.value.reason is RejectReason.UNSUPPORTED_TYPE


async def test_sandbox_child_dying_without_answer_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ребёнок умер молча (лимит, сломанный образ) — это авария, а не «файл не подходит»."""
    monkeypatch.setattr(sys, "executable", "/usr/bin/false")
    with pytest.raises(SandboxFailedError):
        await prepare_upload_isolated(FilePurpose.MENU_PHOTO, make_png())


# --- Ревью PR #495, Н-1: файл, который повтор не спасёт, — отказ, а не «позже» ---


def _tiff_ifd(entries: list[tuple[int, int, int, bytes]]) -> bytes:
    """Сырой EXIF (TIFF, little-endian, один IFD): тег, тип, число, значение."""
    data_offset = 8 + 2 + len(entries) * 12 + 4
    ifd, extra = struct.pack("<H", len(entries)), b""
    for tag, kind, count, value in entries:
        if len(value) <= 4:
            ifd += struct.pack("<HHI", tag, kind, count) + value.ljust(4, b"\0")
        else:
            ifd += struct.pack("<HHII", tag, kind, count, data_offset + len(extra))
            extra += value
    return b"II*\x00" + struct.pack("<I", 8) + ifd + b"\0\0\0\0" + extra


def make_jpeg_with_broken_exif(size: tuple[int, int] = (40, 20)) -> bytes:
    """Снимок «держали вертикально» (ориентация 6), а разрешение записано строкой:
    так пишут иные телефоны и редакторы. Пересборка EXIF на нём падает `TypeError`."""
    exif = _tiff_ifd(
        [
            (_ORIENTATION, 3, 1, struct.pack("<H", 6)),
            (0x011A, 2, 4, b"72\x00\x00"),  # XResolution типом ASCII
        ]
    )
    out = io.BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(out, format="JPEG", exif=b"Exif\0\0" + exif)
    return out.getvalue()


def test_photo_with_broken_exif_tag_is_rotated_not_failed() -> None:
    prepared = prepare_upload(FilePurpose.GUEST_DOCUMENT, make_jpeg_with_broken_exif())

    image = _webp_of(prepared.data)
    assert image.size == (20, 40)  # повёрнут по ориентации 6
    assert len(image.getexif()) == 0


async def test_sandbox_accepts_photo_with_broken_exif() -> None:
    prepared = await prepare_upload_isolated(
        FilePurpose.GUEST_DOCUMENT, make_jpeg_with_broken_exif()
    )
    assert _webp_of(prepared.data).size == (20, 40)


def _swapped_primaries_profile() -> bytes:
    """Профиль ICC, где красный и синий основные цвета поменяны местами.

    «Красный» пиксель с таким профилем — на самом деле синий: перевод в sRGB
    делает его синим, а выброшенный без перевода профиль оставил бы красным.
    Так тест видит перевод без настоящего профиля Display P3 (его в CI нет).
    """
    data = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    (count,) = struct.unpack(">I", data[128:132])
    tags: dict[bytes, tuple[int, int]] = {}
    for index in range(count):
        start = 132 + 12 * index
        signature, offset, size = struct.unpack(">4sII", data[start : start + 12])
        tags[signature] = (offset, size)
    (red_at, red_size), (blue_at, blue_size) = tags[b"rXYZ"], tags[b"bXYZ"]
    red = bytes(data[red_at : red_at + red_size])
    data[red_at : red_at + red_size] = data[blue_at : blue_at + blue_size]
    data[blue_at : blue_at + blue_size] = red
    data[84:100] = bytes(16)  # id профиля (MD5) — «не посчитан»
    return bytes(data)


def _png_with_profile(icc: bytes) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (255, 0, 0)).save(out, format="PNG", icc_profile=icc)
    return out.getvalue()


def test_colours_follow_embedded_profile_and_profile_is_dropped() -> None:
    """Ревью PR #495, Н-22: фото iPhone (Display P3) без перевода в sRGB — тусклее."""
    prepared = prepare_upload(
        FilePurpose.MENU_PHOTO, _png_with_profile(_swapped_primaries_profile())
    )

    image = _webp_of(prepared.data)
    red, _, blue = image.convert("RGB").getpixel((10, 10))  # type: ignore[misc]
    assert blue > 200 and red < 60
    assert not image.info.get("icc_profile")


def test_unreadable_profile_keeps_photo_as_is() -> None:
    prepared = prepare_upload(FilePurpose.MENU_PHOTO, _png_with_profile(b"not an icc profile"))

    red, _, blue = _webp_of(prepared.data).convert("RGB").getpixel((10, 10))  # type: ignore[misc]
    assert red > 200 and blue < 60


async def test_sandbox_converts_colours_by_profile() -> None:
    prepared = await prepare_upload_isolated(
        FilePurpose.MENU_PHOTO, _png_with_profile(_swapped_primaries_profile())
    )
    red, _, blue = _webp_of(prepared.data).convert("RGB").getpixel((10, 10))  # type: ignore[misc]
    assert blue > 200 and red < 60


def test_broken_docx_is_unsupported_type_not_a_failure() -> None:
    """CRC `[Content_Types].xml` не сходится — архив битый: «не тот формат»."""
    docx = make_docx().replace(b"<Types>", b"<Typez>")

    with pytest.raises(FileRejectedError) as caught:
        prepare_upload(FilePurpose.GUEST_DOCUMENT, docx)

    assert caught.value.reason is RejectReason.UNSUPPORTED_TYPE


def test_child_turns_unexpected_decoder_error_into_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой декодера, которого проверка не ждала, — вердикт ребёнка «не тот формат»."""

    def explode(purpose: FilePurpose, data: bytes) -> None:
        raise TypeError("bad operand type for abs(): 'str'")

    answer = io.BytesIO()
    monkeypatch.setattr(file_sandbox, "prepare_upload", explode)
    monkeypatch.setattr(file_sandbox, "_apply_limits", lambda: None)  # лимиты — не тестам
    monkeypatch.setattr(sys, "argv", ["file_sandbox", FilePurpose.MENU_PHOTO.value])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff\xd8")))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(answer))

    file_sandbox._child_main()

    header, _, _ = answer.getvalue().partition(b"\n")
    assert json.loads(header) == {"rejected": RejectReason.UNSUPPORTED_TYPE.value}


async def test_sandbox_child_that_cannot_start_is_a_sandbox_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Интерпретатора нет — `SandboxFailedError` (у сервиса — 006), а не 500."""
    monkeypatch.setattr(sys, "executable", "/nonexistent/python3")
    with pytest.raises(SandboxFailedError):
        await prepare_upload_isolated(FilePurpose.MENU_PHOTO, make_png())

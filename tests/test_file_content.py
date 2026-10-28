"""Проверка содержимого загружаемого файла и шифрование (spec 0047 §4–§5, §9).

Чистые функции `platform/file_content.py`: без БД и хранилища. Образцы файлов
собираются здесь же (Pillow, zipfile) — ими пользуются и тесты сервиса.
"""

from __future__ import annotations

import io
import sys
import zipfile

import pytest
from PIL import Image

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

"""Разбор недоверенного файла (spec 0047 §3, ADR-022 п. 3): тип по сигнатуре, перекодирование
картинок без метаданных, DOCX без макросов — и что всё это идёт в дочернем процессе.

Функции дочернего процесса проверяются и напрямую (быстро, с покрытием), и через настоящий
`python -I -m …` — чтобы изоляция не была только словами в докстринге.
"""

from __future__ import annotations

import io
import sys

import pytest
from PIL import Image

from hospitality.platform import file_inspection, file_sanitizer
from hospitality.platform.file_inspection import FileInspectionError, FileKind, sanitize, sniff_kind
from hospitality.platform.file_sanitizer import FileRejectedError, inspect_docx, transcode_image
from tests import file_samples


def _open(webp: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(webp))
    assert image.format == "WEBP"
    return image


# --- Сигнатуры ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (file_samples.png_with_alpha(), FileKind.IMAGE),
        (file_samples.jpeg_rotated_by_exif(), FileKind.IMAGE),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", FileKind.IMAGE),
        (file_samples.PDF, FileKind.PDF),
        (file_samples.docx(), FileKind.DOCX),
        (file_samples.SVG, None),
        (b"<!doctype html><script>", None),
        (b"MZ\x90\x00", None),  # исполняемый Windows
        (b"\x7fELF", None),
        (b"GIF89a", None),
        (b"plain text renamed to photo.jpg", None),
        (b"", None),
    ],
)
def test_kind_is_sniffed_from_content(data: bytes, expected: FileKind | None) -> None:
    assert sniff_kind(data) == expected


# --- Картинки -----------------------------------------------------------------------------


def test_image_becomes_webp_within_side_without_exif() -> None:
    webp = transcode_image(file_samples.png_with_exif(3000, 1500), 960)

    image = _open(webp)
    assert image.size == (960, 480)
    assert "exif" not in image.info
    assert len(image.getexif()) == 0


def test_image_is_rotated_by_exif_before_metadata_is_dropped() -> None:
    image = _open(transcode_image(file_samples.jpeg_rotated_by_exif(400, 200), 960))

    assert image.size == (200, 400)


def test_small_image_is_not_upscaled_and_keeps_alpha() -> None:
    image = _open(transcode_image(file_samples.png_with_alpha(), 960))

    assert image.size == (50, 50)
    assert image.mode == "RGBA"


def test_image_over_pixel_limit_is_rejected() -> None:
    with pytest.raises(FileRejectedError) as rejection:
        transcode_image(file_samples.huge_png(), 960)
    assert rejection.value.reason == "too_many_pixels"


def test_truncated_image_is_rejected() -> None:
    data = file_samples.png_with_exif(200, 100)
    with pytest.raises(FileRejectedError) as rejection:
        transcode_image(data[: len(data) // 2], 960)
    assert rejection.value.reason == "undecodable"


# --- DOCX ---------------------------------------------------------------------------------


def test_plain_docx_is_accepted() -> None:
    inspect_docx(file_samples.docx())


@pytest.mark.parametrize(
    "data",
    [
        file_samples.docx(extra_names=("word/vbaProject.bin",)),
        file_samples.macro_enabled_docx(),
    ],
    ids=["vbaProject", "macroEnabled"],
)
def test_docx_with_macros_is_rejected(data: bytes) -> None:
    with pytest.raises(FileRejectedError) as rejection:
        inspect_docx(data)
    assert rejection.value.reason == "macros"


@pytest.mark.parametrize("data", [file_samples.plain_zip(), b"PK\x03\x04broken"])
def test_zip_that_is_not_docx_is_rejected(data: bytes) -> None:
    with pytest.raises(FileRejectedError) as rejection:
        inspect_docx(data)
    assert rejection.value.reason == "not_docx"


# --- Точка входа и дочерний процесс -------------------------------------------------------


def test_main_reports_rejection_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(file_samples.plain_zip())))

    assert file_sanitizer.main(["docx"]) == file_sanitizer.EXIT_REJECTED
    assert capsys.readouterr().err.strip() == "not_docx"


async def test_image_is_transcoded_in_child_process() -> None:
    webp = await sanitize(FileKind.IMAGE, file_samples.png_with_exif(3000, 1500), 2048)

    assert _open(webp).size == (2048, 1024)


async def test_child_process_rejection_reaches_parent() -> None:
    with pytest.raises(FileInspectionError) as rejection:
        await sanitize(FileKind.DOCX, file_samples.macro_enabled_docx(), 960)
    assert rejection.value.reason == "macros"


async def test_pdf_is_stored_as_is_without_parsing() -> None:
    assert await sanitize(FileKind.PDF, file_samples.PDF, 960) == file_samples.PDF


async def test_child_process_timeout_rejects_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_inspection, "SANITIZER_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(FileInspectionError) as rejection:
        await sanitize(FileKind.IMAGE, file_samples.png_with_exif(), 960)
    assert rejection.value.reason == "timeout"


async def test_child_process_crash_rejects_file() -> None:
    with pytest.raises(FileInspectionError) as rejection:
        await file_inspection._run_sanitizer(["unknown-mode"], b"")
    assert rejection.value.reason == "sanitizer_exit_2"

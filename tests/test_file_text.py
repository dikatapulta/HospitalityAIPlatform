"""Текст документа гостя: `platform/file_text.py` и операция песочницы (spec 0049 Р-4)."""

from __future__ import annotations

import io
import sys
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from hospitality.modules.documents.tests.conftest import (
    make_blank_pdf,
    make_requisites_docx,
    make_text_pdf,
)
from hospitality.platform.file_sandbox import (
    SandboxFailedError,
    check_sandbox,
    extract_text_isolated,
)
from hospitality.platform.file_text import MAX_CHARS, MAX_PAGES, TextState, extract_text

BILLION_LAUGHS = '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>'


def test_pdf_text_layer_is_read() -> None:
    extracted = extract_text(make_text_pdf(["BIN 940140000385", "BIK HSBKKZKX"]))

    assert extracted.state is TextState.TEXT
    assert "940140000385" in extracted.text
    assert "HSBKKZKX" in extracted.text


def test_scan_without_text_layer_is_no_text() -> None:
    assert extract_text(make_blank_pdf()).state is TextState.NO_TEXT


def test_pdf_pages_beyond_the_limit_are_not_read() -> None:
    writer = PdfWriter()
    for _ in range(MAX_PAGES):
        writer.add_blank_page(width=200, height=200)
    tail = PdfWriter(io.BytesIO(make_text_pdf(["BIN 940140000385"])))
    writer.add_page(tail.pages[0])
    out = io.BytesIO()
    writer.write(out)

    assert extract_text(out.getvalue()).state is TextState.NO_TEXT


def test_docx_table_cells_share_a_line() -> None:
    extracted = extract_text(make_requisites_docx([("БИН", "940140000385"), ("Банк", "Halyk")]))

    assert extracted.state is TextState.TEXT
    assert "БИН\t940140000385\t" in extracted.text
    assert "Банк\tHalyk\t" in extracted.text


def test_docx_with_dtd_is_refused() -> None:
    docx = make_requisites_docx([("БИН", "&lol2;")], doctype=BILLION_LAUGHS)

    assert extract_text(docx).state is TextState.FAILED


def test_broken_or_foreign_files_fail_without_raising() -> None:
    zip_without_word = io.BytesIO()
    with zipfile.ZipFile(zip_without_word, "w") as archive:
        archive.writestr("word/document.xml", b"<not-closed")

    assert extract_text(b"%PDF-1.4 garbage").state is TextState.FAILED
    assert extract_text(zip_without_word.getvalue()).state is TextState.FAILED
    assert extract_text(b"\x89PNG\r\n").state is TextState.FAILED


def test_text_is_capped() -> None:
    docx = make_requisites_docx([("x", "a" * (MAX_CHARS + 500))])

    assert len(extract_text(docx).text) == MAX_CHARS


async def test_sandbox_extracts_in_a_child_process() -> None:
    """Сквозь настоящий дочерний процесс: PDF, DOCX с DTD, скан."""
    pdf = await extract_text_isolated(make_text_pdf(["IIK KZ86125KZT5004100100"]))
    dtd = await extract_text_isolated(
        make_requisites_docx([("x", "&lol2;")], doctype=BILLION_LAUGHS)
    )
    scan = await extract_text_isolated(make_blank_pdf())

    assert pdf.state is TextState.TEXT
    assert "KZ86125KZT5004100100" in pdf.text
    assert dtd.state is TextState.FAILED
    assert scan.state is TextState.NO_TEXT


async def test_sandbox_probe_tells_a_live_sandbox_from_a_broken_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #511: ребёнок умер на файле — проба на пустом входе решает, файл это или среда."""
    await check_sandbox()  # живая песочница — проба молчит

    monkeypatch.setattr(sys, "executable", "/usr/bin/false")
    with pytest.raises(SandboxFailedError):
        await check_sandbox()


async def test_sandbox_keeps_only_the_tail_of_a_flooding_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #511: PDF в 15 КБ давал 94 МБ предупреждений pypdf в stderr, и родитель
    держал их целиком (`communicate`). Теперь — только хвост для диагноза."""
    flood = tmp_path / "flood.sh"
    flood.write_text(
        "#!/bin/sh\nhead -c 20000000 /dev/zero | tr '\\0' x >&2\necho END >&2\nexit 3\n"
    )
    flood.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(flood))

    tracemalloc.start()
    try:
        with pytest.raises(SandboxFailedError) as caught:
            await extract_text_isolated(b"%PDF-1.4")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "END" in str(caught.value)
    assert peak < 5_000_000  # 20 МБ stderr в памяти родителя не было

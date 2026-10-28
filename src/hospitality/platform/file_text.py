"""Текст документа гостя для разбора реквизитов (spec 0049 Р-4, FOUNDATION §11).

Чистые функции без БД и без хранилища; исполняются только в дочернем процессе
песочницы (`file_sandbox.extract_text_isolated`) — разбор PDF и DOCX не идёт в
процессе, который обслуживает гостей.

PDF — `pypdf`, первые `MAX_PAGES` страниц. DOCX — `zipfile` и потоковый
XML-парсер expat: документ с DTD отвергается до разбора тела (ни внутренних
сущностей — «миллиарда смешков», ни внешних). Текст обрезается до `MAX_CHARS`.
Скан без текстового слоя — `no_text`: поля из него заполнит человек (PR 5 — модель).
"""

from __future__ import annotations

import enum
import io
import re
import zipfile
from dataclasses import dataclass
from xml.parsers import expat

from pypdf import PasswordType, PdfReader

MAX_PAGES = 20
MAX_CHARS = 100_000
# Потолок одной XML-части DOCX после распаковки: страницы текста — это
# килобайты, десятки мегабайт — только zip-бомба.
_DOCX_PART_MAX_BYTES = 20 * 1024 * 1024
_DOCX_PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml")
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "


class TextState(enum.StrEnum):
    """Исход извлечения текста (колонка `act_request_files.text_state`)."""

    TEXT = "text"
    NO_TEXT = "no_text"
    FAILED = "failed"


@dataclass(frozen=True)
class ExtractedText:
    state: TextState
    text: str = ""


class _ForbiddenDtdError(Exception):
    """В XML есть DTD — такой DOCX не разбирается."""


def extract_text(data: bytes) -> ExtractedText:
    """Текст PDF или DOCX; другой формат или битый файл — `failed`."""
    if data.startswith(b"%PDF-"):
        return _finish(_pdf_text(data))
    if data.startswith(b"PK\x03\x04"):
        return _finish(_docx_text(data))
    return ExtractedText(TextState.FAILED)


def _finish(text: str | None) -> ExtractedText:
    if text is None:
        return ExtractedText(TextState.FAILED)
    text = text[:MAX_CHARS]
    if not text.strip():
        return ExtractedText(TextState.NO_TEXT)
    return ExtractedText(TextState.TEXT, text)


def _pdf_text(data: bytes) -> str | None:
    # Любое исключение разбора — вердикт «не разобран»: файл прислал гость, и
    # перечислять, чем именно ломается враждебный PDF, бессмысленно.
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            return None
        parts: list[str] = []
        total = 0
        for index in range(min(len(reader.pages), MAX_PAGES)):
            page_text = reader.pages[index].extract_text() or ""
            parts.append(page_text)
            total += len(page_text)
            if total >= MAX_CHARS:
                break
    except Exception:
        return None
    return "\n".join(parts)


def _docx_text(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = sorted(
                (name for name in archive.namelist() if _DOCX_PARTS.fullmatch(name)),
                key=lambda name: (not name.endswith("document.xml"), name),
            )
            parts: list[str] = []
            for name in names:
                with archive.open(name) as stream:
                    xml = stream.read(_DOCX_PART_MAX_BYTES + 1)
                if len(xml) > _DOCX_PART_MAX_BYTES:
                    return None
                parts.append(_word_xml_text(xml))
                if sum(len(part) for part in parts) >= MAX_CHARS:
                    break
    except Exception:
        return None
    return "\n".join(parts)


def _word_xml_text(xml: bytes) -> str:
    """Текст части WordprocessingML: абзац — строка, ячейка таблицы — табуляция.

    Метка и значение в соседних ячейках («БИН | 123…») ложатся в одну строку —
    так их находит разбор полей.
    """
    out: list[str] = []
    in_text = False
    cell_depth = 0

    def forbid_dtd(*_: object) -> None:
        raise _ForbiddenDtdError

    def start(name: str, _attrs: dict[str, str]) -> None:
        nonlocal in_text, cell_depth
        if name == _W + "t":
            in_text = True
        elif name == _W + "tab":
            out.append("\t")
        elif name in (_W + "br", _W + "cr"):
            out.append("\n")
        elif name == _W + "tc":
            cell_depth += 1

    def end(name: str) -> None:
        nonlocal in_text, cell_depth
        if name == _W + "t":
            in_text = False
        elif name == _W + "p":
            out.append(" " if cell_depth else "\n")
        elif name == _W + "tc":
            cell_depth -= 1
            while out and out[-1] == " ":  # конец последнего абзаца ячейки
                out.pop()
            out.append("\t")
        elif name == _W + "tr":
            out.append("\n")

    def chars(data: str) -> None:
        if in_text:
            out.append(data)

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = forbid_dtd
    parser.EntityDeclHandler = forbid_dtd
    parser.ExternalEntityRefHandler = forbid_dtd  # type: ignore[assignment]
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(xml, True)
    return "".join(out)
